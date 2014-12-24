import os
import struct
import logging
from collections import defaultdict

from elfesteem import pe
from elfesteem import cstruct
from elfesteem import *

from miasm2.jitter.csts import *
from miasm2.jitter.loader.utils import canon_libname_libfunc, libimp


log = logging.getLogger('loader_pe')
hnd = logging.StreamHandler()
hnd.setFormatter(logging.Formatter("[%(levelname)s]: %(message)s"))
log.addHandler(hnd)
log.setLevel(logging.CRITICAL)

def get_import_address_pe(e):
    import2addr = defaultdict(set)
    if e.DirImport.impdesc is None:
        return import2addr
    for s in e.DirImport.impdesc:
        # fthunk = e.rva2virt(s.firstthunk)
        # l = "%2d %-25s %s" % (i, repr(s.dlldescname), repr(s))
        libname = s.dlldescname.name.lower()
        for ii, imp in enumerate(s.impbynames):
            if isinstance(imp, pe.ImportByName):
                funcname = imp.name
            else:
                funcname = imp
            # l = "    %2d %-16s" % (ii, repr(funcname))
            import2addr[(libname, funcname)].add(
                e.rva2virt(s.firstthunk + e._wsize * ii / 8))
    return import2addr


def preload_pe(vm, e, runtime_lib, patch_vm_imp=True):
    fa = get_import_address_pe(e)
    dyn_funcs = {}
    # log.debug('imported funcs: %s' % fa)
    for (libname, libfunc), ads in fa.items():
        for ad in ads:
            ad_base_lib = runtime_lib.lib_get_add_base(libname)
            ad_libfunc = runtime_lib.lib_get_add_func(ad_base_lib, libfunc, ad)

            libname_s = canon_libname_libfunc(libname, libfunc)
            dyn_funcs[libname_s] = ad_libfunc
            if patch_vm_imp:
                vm.set_mem(
                    ad, struct.pack(cstruct.size2type[e._wsize], ad_libfunc))
    return dyn_funcs



def is_redirected_export(e, ad):
    # test is ad points to code or dll name
    out = ''
    for i in xrange(0x200):
        c = e.virt(ad + i)
        if c == "\x00":
            break
        out += c
        if not (c.isalnum() or c in "_.-+*$@&#()[]={}"):
            return False
    if not "." in out:
        return False
    i = out.find('.')
    return out[:i], out[i + 1:]


def get_export_name_addr_list(e):
    out = []
    # add func name
    for i, n in enumerate(e.DirExport.f_names):
        addr = e.DirExport.f_address[e.DirExport.f_nameordinals[i].ordinal]
        f_name = n.name.name
        # log.debug('%s %s' % (f_name, hex(e.rva2virt(addr.rva))))
        out.append((f_name, e.rva2virt(addr.rva)))

    # add func ordinal
    for i, o in enumerate(e.DirExport.f_nameordinals):
        addr = e.DirExport.f_address[o.ordinal]
        # log.debug('%s %s %s' % (o.ordinal, e.DirExport.expdesc.base,
        # hex(e.rva2virt(addr.rva))))
        out.append(
            (o.ordinal + e.DirExport.expdesc.base, e.rva2virt(addr.rva)))
    return out



def vm_load_pe(vm, fdata, align_s=True, load_hdr=True, **kargs):
    """Load a PE in memory (@vm) from a data buffer @fdata
    @vm: VmMngr instance
    @fdata: data buffer to parse
    @align_s: (optional) If False, keep gaps between section
    @load_hdr: (optional) If False, do not load the NThdr in memory
    Return the corresponding PE instance.

    Extra arguments are passed to PE instanciation.
    If all sections are aligned, they will be mapped on several different pages
    Otherwise, a big page is created, containing all sections
    """
    # Parse and build a PE instance
    pe = pe_init.PE(fdata, **kargs)

    # Check if all section are aligned
    aligned = True
    for section in pe.SHList:
        if section.addr & 0xFFF:
            aligned = False
            break

    if aligned:
        # Loader NT header
        if load_hdr:
            # Header length
            hdr_len = max(0x200, pe.NThdr.sizeofheaders)
            # Page minimum size
            min_len = min(pe.SHList[0].addr, 0x1000)

            # Get and pad the pe_hdr
            pe_hdr = pe.content[:hdr_len] + max(0, (min_len - hdr_len)) * "\x00"
            vm.add_memory_page(pe.NThdr.ImageBase, PAGE_READ | PAGE_WRITE,
                               pe_hdr)

        # Align sections size
        if align_s:
            # Use the next section address to compute the new size
            for i, section in enumerate(pe.SHList[:-1]):
                new_size = pe.SHList[i + 1].addr - section.addr
                section.size = new_size
                section.rawsize = new_size
                section.data = strpatchwork.StrPatchwork(section.data[:new_size])
                section.offset = section.addr

            # Last section alignement
            last_section = pe.SHList[-1]
            last_section.size = (last_section.size + 0xfff) & 0xfffff000

        # Pad sections with null bytes and map them
        for section in pe.SHList:
            data = str(section.data)
            data += "\x00" * (section.size - len(data))
            vm.add_memory_page(pe.rva2virt(section.addr),
                               PAGE_READ | PAGE_WRITE, data)

        return pe

    # At least one section is not aligned
    log.warning('PE is not aligned, creating big section')
    min_addr = 0 if load_hdr else None
    max_addr = None
    data = ""

    for i, section in enumerate(pe.SHList):
        if i < len(pe.SHList) - 1:
            # If it is not the last section, use next section address
            section.size = pe.SHList[i + 1].addr - section.addr
        section.rawsize = section.size
        section.offset = section.addr

        # Update min and max addresses
        if min_addr is None or section.addr < min_addr:
            min_addr = section.addr
        if max_addr is None or section.addr + section.size > max_addr:
            max_addr = section.addr + max(section.size, len(section.data))

    min_addr = pe.rva2virt(min_addr)
    max_addr = pe.rva2virt(max_addr)
    log.debug('Min: 0x%x, Max: 0x%x, Size: 0x%x' % (min_addr, max_addr,
                                                    (max_addr - min_addr)))

    # Create only one big section containing the whole PE
    vm.add_memory_page(min_addr,
                       PAGE_READ | PAGE_WRITE,
                       (max_addr - min_addr) * "\x00")

    # Copy each sections content in memory
    for section in pe.SHList:
        log.debug('Map 0x%x bytes to 0x%x' % (len(s.data), pe.rva2virt(s.addr)))
        vm.set_mem(pe.rva2virt(s.addr), str(s.data))

    return pe


def vm_load_pe_lib(fname_in, libs, lib_path_base, **kargs):
    """Call vm_load_pe on @fname_in and update @libs accordingly
    @fname_in: library name
    @libs: libimp_pe instance
    @lib_path_base: DLLs relative path
    Return the corresponding PE instance
    Extra arguments are passed to vm_load_pe
    """
    fname = os.path.join(lib_path_base, fname_in)
    with open(fname) as fstream:
        pe = vm_load_pe(fstream.read(), **kargs)
    libs.add_export_lib(pe, fname_in)
    return pe


def vm_load_pe_libs(libs_name, libs, lib_path_base="win_dll", **kargs):
    """Call vm_load_pe_lib on each @libs_name filename
    @libs_name: list of str
    @libs: libimp_pe instance
    @lib_path_base: (optional) DLLs relative path
    Return a dictionnary Filename -> PE instances
    Extra arguments are passed to vm_load_pe_lib
    """
    return {fname: vm_load_pe_lib(fname, libs, lib_path_base, **kargs)
            for fname in libs_name}


def vm_fix_imports_pe_libs(lib_imgs, libs, lib_path_base="win_dll",
                           patch_vm_imp=True, **kargs):
    for e in lib_imgs.values():
        preload_pe(e, libs, patch_vm_imp)


def vm2pe(myjit, fname, libs=None, e_orig=None,
          min_addr=None, max_addr=None,
          min_section_offset=0x1000, img_base=None,
          added_funcs=None):
    mye = pe_init.PE()

    if min_addr is None and e_orig is not None:
        min_addr = min([e_orig.rva2virt(s.addr) for s in e_orig.SHList])
    if max_addr is None and e_orig is not None:
        max_addr = max([e_orig.rva2virt(s.addr + s.size) for s in e_orig.SHList])


    if img_base is None:
        img_base = e_orig.NThdr.ImageBase

    mye.NThdr.ImageBase = img_base
    all_mem = myjit.vm.get_all_memory()
    addrs = all_mem.keys()
    addrs.sort()
    mye.Opthdr.AddressOfEntryPoint = mye.virt2rva(myjit.cpu.EIP)
    first = True
    for ad in addrs:
        if not min_addr <= ad < max_addr:
            continue
        log.debug('%s' % hex(ad))
        if first:
            mye.SHList.add_section(
                "%.8X" % ad,
                addr=ad - mye.NThdr.ImageBase,
                data=all_mem[ad]['data'],
                offset=min_section_offset)
        else:
            mye.SHList.add_section(
                "%.8X" % ad,
                addr=ad - mye.NThdr.ImageBase,
                data=all_mem[ad]['data'])
        first = False
    if libs:
        if added_funcs is not None:
            # name_inv = dict([(x[1], x[0]) for x in libs.name2off.items()])

            for addr, funcaddr in added_func:
                libbase, dllname = libs.fad2info[funcaddr]
                libs.lib_get_add_func(libbase, dllname, addr)

        new_dll = libs.gen_new_lib(mye, lambda x: mye.virt.is_addr_in(x))
    else:
        new_dll = {}

    log.debug('%s' % new_dll)

    mye.DirImport.add_dlldesc(new_dll)
    s_imp = mye.SHList.add_section("import", rawsize=len(mye.DirImport))
    mye.DirImport.set_rva(s_imp.addr)
    log.debug('%s' % repr(mye.SHList))
    if e_orig:
        # resource
        xx = str(mye)
        mye.content = xx
        ad = e_orig.NThdr.optentries[pe.DIRECTORY_ENTRY_RESOURCE].rva
        log.debug('dirres %s' % hex(ad))
        if ad != 0:
            mye.NThdr.optentries[pe.DIRECTORY_ENTRY_RESOURCE].rva = ad
            mye.DirRes = pe.DirRes.unpack(xx, ad, mye)
            # log.debug('%s' % repr(mye.DirRes))
            s_res = mye.SHList.add_section(
                name="myres", rawsize=len(mye.DirRes))
            mye.DirRes.set_rva(s_res.addr)
            log.debug('%s' % repr(mye.DirRes))
    # generation
    open(fname, 'w').write(str(mye))
    return mye


class libimp_pe(libimp):

    def add_export_lib(self, e, name):
        self.all_exported_lib.append(e)
        # will add real lib addresses to database
        if name in self.name2off:
            ad = self.name2off[name]
        else:
            log.debug('new lib %s' % name)
            ad = e.NThdr.ImageBase
            libad = ad
            self.name2off[name] = ad
            self.libbase2lastad[ad] = ad + 0x1
            self.lib_imp2ad[ad] = {}
            self.lib_imp2dstad[ad] = {}
            self.libbase_ad += 0x1000

            ads = get_export_name_addr_list(e)
            todo = ads
            # done = []
            while todo:
                # for imp_ord_or_name, ad in ads:
                imp_ord_or_name, ad = todo.pop()

                # if export is a redirection, search redirected dll
                # and get function real addr
                ret = is_redirected_export(e, ad)
                if ret:
                    exp_dname, exp_fname = ret
                    # log.debug('export redirection %s' % imp_ord_or_name)
                    # log.debug('source %s %s' % (exp_dname, exp_fname))
                    exp_dname = exp_dname + '.dll'
                    exp_dname = exp_dname.lower()
                    # if dll auto refes in redirection
                    if exp_dname == name:
                        libad_tmp = self.name2off[exp_dname]
                        if not exp_fname in self.lib_imp2ad[libad_tmp]:
                            # schedule func
                            todo = [(imp_ord_or_name, ad)] + todo
                            continue
                    elif not exp_dname in self.name2off:
                        raise ValueError('load %r first' % exp_dname)
                    c_name = canon_libname_libfunc(exp_dname, exp_fname)
                    libad_tmp = self.name2off[exp_dname]
                    ad = self.lib_imp2ad[libad_tmp][exp_fname]
                    # log.debug('%s' % hex(ad))
                # if not imp_ord_or_name in self.lib_imp2dstad[libad]:
                #    self.lib_imp2dstad[libad][imp_ord_or_name] = set()
                # self.lib_imp2dstad[libad][imp_ord_or_name].add(dst_ad)

                # log.debug('new imp %s %s' % (imp_ord_or_name, hex(ad)))
                self.lib_imp2ad[libad][imp_ord_or_name] = ad

                name_inv = dict([(x[1], x[0]) for x in self.name2off.items()])
                c_name = canon_libname_libfunc(
                    name_inv[libad], imp_ord_or_name)
                self.fad2cname[ad] = c_name
                self.fad2info[ad] = libad, imp_ord_or_name

    def gen_new_lib(self, target_pe, filter=lambda _: True):
        """Gen a new DirImport description
        @target_pe: PE instance
        @filter: (boolean f(address)) restrict addresses to keep
        """

        new_lib = []
        for lib_name, ad in self.name2off.items():
            # Build an IMAGE_IMPORT_DESCRIPTOR

            # Get fixed addresses
            out_ads = dict() # addr -> func_name
            for func_name, dst_addresses in self.lib_imp2dstad[ad].items():
                out_ads.update({addr:func_name for addr in dst_addresses})

            # Filter available addresses according to @filter
            all_ads = [addr for addr in out_ads.keys() if filter(addr)]
            log.debug('ads: %s' % map(hex, all_ads))
            if not all_ads:
                continue

            # Keep non-NULL elements
            all_ads.sort()
            for i, x in enumerate(all_ads):
                if x not in [0,  None]:
                    break
            all_ads = all_ads[i:]

            while all_ads:
                # Find libname's Import Address Table
                othunk = all_ads[0]
                i = 0
                while i + 1 < len(all_ads) and all_ads[i] + 4 == all_ads[i + 1]:
                    i += 1
                # 'i + 1' is IAT's length

                # Effectively build an IMAGE_IMPORT_DESCRIPTOR
                funcs = [out_ads[addr] for addr in all_ads[:i + 1]]
                try:
                    rva = target_pe.virt2rva(othunk)
                except pe.InvalidOffset:
                    pass
                else:
                    new_lib.append(({"name": lib_name,
                                     "firstthunk": rva},
                                    funcs)
                                   )

                # Update elements to handle
                all_ads = all_ads[i + 1:]

        return new_lib
