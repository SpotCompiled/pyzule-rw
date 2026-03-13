import os
import sys
import struct
import subprocess

from cyan import tbhutils


class Executable:
  FAT_MAGIC = 0xcafebabe
  FAT_CIGAM = 0xbebafeca
  FAT_MAGIC_64 = 0xcafebabf
  FAT_CIGAM_64 = 0xbfbafeca
  MH_MAGIC_64 = 0xfeedfacf
  MH_CIGAM_64 = 0xcffaedfe
  LC_BUILD_VERSION = 0x32
  LC_VERSION_MIN_IPHONEOS = 0x25
  IOS26_PACKED = 26 << 16

  install_dir, specific = tbhutils.get_tools_dir()
  nt = f"{specific}/install_name_tool"
  ldid = f"{specific}/ldid"
  lipo = f"{specific}/lipo"
  otool = f"{specific}/otool"
  idylib = f"{specific}/insert_dylib"

  # adding /usr/lib/ now, idk why i didnt before. lets hope nothing breaks
  ## LITERALLY 2 DAYS LATER. WHAT THE FUCK IS @LOADER_PATH HELP
  ## i will cry if only checking for '@' will break this.
  starters = ("\t/Library/", "\t/usr/lib/", "\t@")

  # substrate could show up as
  # CydiaSubstrate.framework, libsubstrate.dylib, EVEN CydiaSubstrate.dylib
  # AND PROBABLY EVEN MORE !!!! IT'S CRAZY.
  common = {
    "substrate.": {
      "name": "CydiaSubstrate.framework",
      "path": "@rpath/CydiaSubstrate.framework/CydiaSubstrate"
    }
  }

  def __init__(self, path: str):
    if not os.path.isfile(path):
      print(f"[!] {path} does not exist (executable)", file=sys.stderr)
      sys.exit(
        "[?] check the wiki for info: "
        "https://github.com/asdfzxcvbn/pyzule-rw/wiki/"
        "file-does-not-exist-(executable)-%3F"
      )

    self.path = path
    self.bn = os.path.basename(path)

  def is_encrypted(self) -> bool:
    proc = subprocess.run(
      [self.otool, "-l", self.path],
      capture_output=True
    )

    return b"cryptid 1" in proc.stdout

  def remove_signature(self) -> None:
    subprocess.run([self.ldid, "-R", self.path], stderr=subprocess.DEVNULL)

  def fakesign(self) -> bool:
    return subprocess.run([self.ldid, "-S", "-M", self.path]).returncode == 0

  def thin(self) -> bool:
    return subprocess.run(
      [self.lipo, "-thin", "arm64", self.path, "-output", self.path],
      stderr=subprocess.DEVNULL
    ).returncode == 0

  def change_dependency(self, old: str, new: str) -> None:
    subprocess.run(
      [self.nt, "-change", old, new, self.path],
      stderr=subprocess.DEVNULL
    )

  def fix_common_dependencies(self, needed: set[str]) -> None:
    self.remove_signature()

    for dep in self.get_dependencies():
      for common, info in self.common.items():
        if common in dep.lower():
          needed.add(common)

          if dep != info["path"]:
            self.change_dependency(dep, info["path"])
            print(
              f"[*] fixed common dependency in {self.bn}: "
              f"{dep} -> {info['path']}"
            )

  def fix_dependencies(self, tweaks: dict[str, str]) -> None:
    for dep in self.get_dependencies():
      for cname in tweaks:
        if cname in dep:
          # i wonder if there's a better way to do this?
          if cname.endswith(".framework"):
            # nah, not gonna parse the plist,
            # i've never seen a framework with a "mismatched" name
            npath = f"@rpath/{cname}/{cname[:-10]}"
          else:
            npath = f"@rpath/{cname}"

          if dep != npath:
            self.change_dependency(dep, npath)
            print(f"[*] fixed dependency in {self.bn}: {dep} -> {npath}")

  def get_dependencies(self) -> list[str]:
    proc = subprocess.run(
      [self.otool, "-L", self.path],
      capture_output=True, text=True
    )

    # split at [2:] to avoid otool's line and dylib's id
    tmp = proc.stdout.strip().split("\n")[2:]
    for ind, dep in enumerate(tmp):
      if "(architecture " in dep:  # avoid checking duplicate deps
        tmp = tmp[:ind]

    deps: list[str] = []
    for dep in tmp:
      if any(dep.startswith(s) for s in self.starters):
        deps.append(dep.split()[0])  # split() removes whitespace

    return deps

  def get_rpaths(self) -> list[str]:
    proc = subprocess.run(
      [self.otool, "-l", self.path],
      capture_output=True, text=True
    )

    if proc.returncode != 0:
      return []

    rpaths: list[str] = []
    lines = proc.stdout.splitlines()
    for i, line in enumerate(lines):
      if line.strip() != "cmd LC_RPATH":
        continue

      for j in range(i + 1, min(i + 8, len(lines))):
        s = lines[j].strip()
        if s.startswith("path "):
          rpaths.append(s.split(" (offset", 1)[0][5:])
          break

    return rpaths

  def ensure_single_rpath(self, rpath: str) -> bool:
    existing = self.get_rpaths()
    count = existing.count(rpath)
    changed = False

    if count == 0:
      add = subprocess.run(
        [self.nt, "-add_rpath", rpath, self.path],
        stderr=subprocess.DEVNULL
      )

      if add.returncode == 0:
        return True

      print(f"[!] failed to add LC_RPATH {rpath} in {self.bn}")
      return False

    while count > 1:
      dele = subprocess.run(
        [self.nt, "-delete_rpath", rpath, self.path],
        stderr=subprocess.DEVNULL
      )

      if dele.returncode != 0:
        print(f"[!] failed to remove duplicate LC_RPATH {rpath} in {self.bn}")
        break

      changed = True
      count -= 1

    return changed

  def patch_sdk26(self) -> bool:
    try:
      with open(self.path, "rb+") as f:
        data = bytearray(f.read())

        if not self._patch_macho_sdk26(data):
          return False

        f.seek(0)
        f.write(data)
        f.truncate()

      print(f"[*] patched SDK/build target to iOS 26 in {self.bn}")
      return True
    except OSError as e:
      print(f"[!] failed to patch {self.bn}: {e}")
      return False

  def _patch_macho_sdk26(self, data: bytearray) -> bool:
    if len(data) < 4:
      return False

    magic = struct.unpack_from(">I", data, 0)[0]
    if magic in (self.FAT_MAGIC, self.FAT_CIGAM, self.FAT_MAGIC_64, self.FAT_CIGAM_64):
      return self._patch_fat_macho_sdk26(data, magic)

    return self._patch_macho_slice_sdk26(data, 0)

  def _patch_fat_macho_sdk26(self, data: bytearray, magic: int) -> bool:
    if magic in (self.FAT_MAGIC, self.FAT_MAGIC_64):
      endian = ">"
    else:
      endian = "<"

    is_fat64 = magic in (self.FAT_MAGIC_64, self.FAT_CIGAM_64)
    header_size = 8
    arch_size = 32 if is_fat64 else 20

    if len(data) < header_size:
      return False

    nfat = struct.unpack_from(f"{endian}I", data, 4)[0]
    changed = False

    for i in range(nfat):
      arch_off = header_size + (i * arch_size)
      if arch_off + arch_size > len(data):
        break

      if is_fat64:
        offset = struct.unpack_from(f"{endian}Q", data, arch_off + 8)[0]
      else:
        offset = struct.unpack_from(f"{endian}I", data, arch_off + 8)[0]

      if self._patch_macho_slice_sdk26(data, offset):
        changed = True

    return changed

  def _patch_macho_slice_sdk26(self, data: bytearray, offset: int) -> bool:
    mh64_size = 32
    if offset < 0 or (offset + mh64_size) > len(data):
      return False

    magic_le = struct.unpack_from("<I", data, offset)[0]
    magic_be = struct.unpack_from(">I", data, offset)[0]

    if magic_le == self.MH_MAGIC_64:
      endian = "<"
    elif magic_be == self.MH_CIGAM_64:
      endian = ">"
    else:
      return False

    ncmds = struct.unpack_from(f"{endian}I", data, offset + 16)[0]
    cmd_off = offset + mh64_size
    changed = False

    for _ in range(ncmds):
      if cmd_off + 8 > len(data):
        break

      cmd, cmdsize = struct.unpack_from(f"{endian}II", data, cmd_off)
      if cmdsize < 8 or cmd_off + cmdsize > len(data):
        break

      if cmd == self.LC_BUILD_VERSION and cmdsize >= 24:
        minos = struct.unpack_from(f"{endian}I", data, cmd_off + 12)[0]
        sdk = struct.unpack_from(f"{endian}I", data, cmd_off + 16)[0]

        if minos != self.IOS26_PACKED:
          struct.pack_into(f"{endian}I", data, cmd_off + 12, self.IOS26_PACKED)
          changed = True

        if sdk != self.IOS26_PACKED:
          struct.pack_into(f"{endian}I", data, cmd_off + 16, self.IOS26_PACKED)
          changed = True
      elif cmd == self.LC_VERSION_MIN_IPHONEOS and cmdsize >= 16:
        version = struct.unpack_from(f"{endian}I", data, cmd_off + 8)[0]
        sdk = struct.unpack_from(f"{endian}I", data, cmd_off + 12)[0]

        if version != self.IOS26_PACKED:
          struct.pack_into(f"{endian}I", data, cmd_off + 8, self.IOS26_PACKED)
          changed = True

        if sdk != self.IOS26_PACKED:
          struct.pack_into(f"{endian}I", data, cmd_off + 12, self.IOS26_PACKED)
          changed = True

      cmd_off += cmdsize

    return changed

