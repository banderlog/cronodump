import io
import struct
import zlib
from .hexdump import tohex, toout
import crodump.koddecoder


class Datafile:
    """Represent a single .dat with it's .tad index file"""

    def __init__(self, name, dat, tad, compact, kod):
        self.name = name
        self.dat = dat
        self.tad = tad
        self.compact = compact

        self.readdathdr()
        self.readtad()

        self.dat.seek(0, io.SEEK_END)
        self.datsize = self.dat.tell()

        self.kod = kod if not kod or self.isencrypted() else crodump.koddecoder.new()

    def isencrypted(self):
        return self.version in (b'01.04', b'01.05') or self.isv4()

    def isv3(self):
        #  01.02: 32 bit file offsets
        #  01.03: 64 bit file offsets
        #  01.04:  encrypted?, 32bit
        #  01.05:  encrypted?, 64bit
        return self.version in (b'01.02', b'01.03', b'01.04', b'01.05')

    def isv4(self):
        #  01.11  v4 ( 64bit )
        #  01.14  v4 ( 64bit ), encrypted?
        #  01.13  ?? I have not seen this version anywhere yet.
        return self.version in (b'01.11', b'01.13', b'01.14')

    def isv7(self):
        #  01.19  ?? I have not seen this version anywhere yet.
        return self.version in (b'01.19',)

    def readdathdr(self):
        """
        Read the .dat file header.
        Note that the 19 byte header if followed by 0xE9 random bytes, generated by
        'srand(time())' followed by 0xE9 times obfuscate(rand())
        """
        self.dat.seek(0)
        hdrdata = self.dat.read(19)

        (
            magic,            # +00  8 bytes
            self.hdrunk,      # +08  uint16
            self.version,     # +0a  5 bytes
            self.encoding,    # +0f  uint16
            self.blocksize,   # +11  uint16
        ) = struct.unpack("<8sH5sHH", hdrdata)

        if magic != b"CroFile\x00":
            print("unknown magic: ", magic)
            raise Exception("not a Crofile")
        self.use64bit = self.version in (b"01.03", b"01.05", b"01.11")

        # blocksize
        #   0040 -> Bank
        #   0400 -> Index or Sys
        #   0200 -> Stru  or Sys

        # encoding
        #   bit0 = 'KOD encoded'
        #   bit1 = compressed

    def readtad(self):
        """
        read and decode the .tad file.
        """
        self.tad.seek(0)
        if self.isv3():
            hdrdata = self.tad.read(2 * 4)
            self.nrdeleted, self.firstdeleted = struct.unpack("<2L", hdrdata)
        elif self.isv4():
            hdrdata = self.tad.read(4 * 4)
            unk1, self.nrdeleted, self.firstdeleted, unk2 = struct.unpack("<4L", hdrdata)
        else:
            raise Exception("unsupported .tad version")

        self.tadhdrlen = self.tad.tell()
        self.tadentrysize = 16 if self.use64bit else 12
        if self.compact:
            self.tad.seek(0, io.SEEK_END)
        else:
            self.idxdata = self.tad.read()
        self.tadsize = self.tad.tell() - self.tadhdrlen
        self.nrofrecords = self.tadsize // self.tadentrysize
        if self.tadsize % self.tadentrysize:
            print("WARN: leftover data in .tad")

    def tadidx(self, idx):
        """
        If we're not supposed to be more compact but slower, lookup from a cached .tad
        """
        if self.compact:
            return self.tadidx_seek(idx)

        if self.use64bit:
            # 01.03 and 01.11 have 64 bit file offsets
            return struct.unpack_from("<QLL", self.idxdata, idx * self.tadentrysize)
        else:
            # 01.02  and 01.04  have 32 bit offsets.
            return struct.unpack_from("<LLL", self.idxdata, idx * self.tadentrysize)

    def tadidx_seek(self, idx):
        """
            Memory saving version without caching the .tad
        """
        self.tad.seek(self.tadhdrlen + idx * self.tadentrysize)
        idxdata = self.tad.read(self.tadentrysize)

        if self.use64bit:
            # 01.03 and 01.11 have 64 bit file offsets
            return struct.unpack("<QLL", idxdata)
        else:
            # 01.02  and 01.04  have 32 bit offsets.
            return struct.unpack("<LLL", idxdata)

    def readdata(self, ofs, size):
        """
        Read raw data from the .dat file
        """
        self.dat.seek(ofs)
        return self.dat.read(size)

    def readrec(self, idx):
        """
        Extract and decode a single record.
        """
        if idx == 0:
            raise Exception("recnum must be a positive number")
        ofs, ln, chk = self.tadidx(idx - 1)
        if ln == 0xFFFFFFFF:
            # deleted record
            return

        if self.isv3():
            flags = ln >> 24
            ln &= 0xFFFFFFF
        elif self.isv4():
            flags = ofs >> 56
            ofs &= (1 << 56) - 1

        dat = self.readdata(ofs, ln)

        if not dat:
            # empty record
            encdat = dat
        elif not flags:
            if self.use64bit:
                extofs, extlen = struct.unpack("<QL", dat[:12])
                o = 12
            else:
                extofs, extlen = struct.unpack("<LL", dat[:8])
                o = 8

            encdat = dat[o:]
            while len(encdat) < extlen:
                dat = self.readdata(extofs, self.blocksize)
                if self.use64bit:
                    (extofs,) = struct.unpack("<Q", dat[:8])
                    o = 8
                else:
                    (extofs,) = struct.unpack("<L", dat[:4])
                    o = 4
                encdat += dat[o:]

            encdat = encdat[:extlen]
        else:
            encdat = dat

        if self.encoding & 1:
            if self.kod:
                encdat = self.kod.decode(idx, encdat)

        if self.iscompressed(encdat):
            encdat = self.decompress(encdat)

        return encdat

    def enumrecords(self):
        for i in range(self.nrofrecords):
            yield self.readrec(i + 1)

    def enumunreferenced(self, ranges, filesize):
        """
        From a list of used byte ranges and the filesize, enumerate the list of unused byte ranges
        """
        o = 0
        for start, end, desc in sorted(ranges):
            if start > o:
                yield o, start - o
            o = end
        if o < filesize:
            yield o, filesize - o

    def dump(self, args):
        """
        Dump decodes all data referenced from the .tad file.
        And optionally print out all unreferenced byte ranges in the .dat file.

        This function is mostly useful for reverse-engineering the database format.

        the `args` object controls how data is decoded.
        """
        print("hdr: %-6s dat: %04x %s enc:%04x bs:%04x, tad: %08x %08x" % (
              self.name, self.hdrunk, self.version,
              self.encoding, self.blocksize,
              self.nrdeleted, self.firstdeleted))

        ranges = []  # keep track of used bytes in the .dat file.

        for i in range(self.nrofrecords):
            (ofs, ln, chk) = self.tadidx(i)
            idx = i + 1
            if args.maxrecs and (i == args.maxrecs):
                break
            if ln == 0xFFFFFFFF:
                print("%5d: %08x %08x %08x" % (idx, ofs, ln, chk))
                continue

            if self.isv3():
                flags = ln >> 24
                ln &= 0xFFFFFFF
            elif self.isv4():
                flags = ofs >> 56
                # 04 --> data, v3compdata
                # 02,03 --> deleted
                # 00 --> extrec
                ofs &= (1 << 56) - 1

            dat = self.readdata(ofs, ln)
            ranges.append((ofs, ofs + ln, "item #%d" % i))
            decflags = [" ", " "]
            infostr = ""
            tail = b""

            if not dat:
                # empty record
                encdat = dat
            elif not flags:
                if self.use64bit:
                    extofs, extlen = struct.unpack("<QL", dat[:12])
                    o = 12
                else:
                    extofs, extlen = struct.unpack("<LL", dat[:8])
                    o = 8
                infostr = "%08x;%08x" % (extofs, extlen)
                encdat = dat[o:]
                while len(encdat) < extlen:
                    dat = self.readdata(extofs, self.blocksize)
                    ranges.append((extofs, extofs + self.blocksize, "item #%d ext" % i))
                    if self.use64bit:
                        (extofs,) = struct.unpack("<Q", dat[:8])
                        o = 8
                    else:
                        (extofs,) = struct.unpack("<L", dat[:4])
                        o = 4
                    infostr += ";%08x" % (extofs)
                    encdat += dat[o:]
                tail = encdat[extlen:]
                encdat = encdat[:extlen]
                decflags[0] = "+"
            else:
                encdat = dat
                decflags[0] = "*"

            if self.encoding & 1:
                if self.kod:
                    encdat = self.kod.decode(idx, encdat)
            else:
                decflags[0] = " "

            if args.decompress:
                if self.iscompressed(encdat):
                    encdat = self.decompress(encdat)
                    decflags[1] = "@"

            # TODO: separate handling for v4
            print("%5d: %08x-%08x: (%02x:%08x) %s %s%s %s" % (i + 1, ofs, ofs + ln, flags, chk, infostr, "".join(decflags), toout(args, encdat), tohex(tail)))

        if args.verbose:
            # output parts not referenced in the .tad file.
            for o, l in self.enumunreferenced(ranges, self.datsize):
                dat = self.readdata(o, l)
                print("%08x-%08x: %s" % (o, o + l, toout(args, dat)))

    def iscompressed(self, data):
        """
        Check if this record looks like a compressed record.
        """
        if len(data) < 11:
            return
        if data[-3:] != b"\x00\x00\x02":
            return
        o = 0
        while o < len(data) - 3:
            size, flag = struct.unpack_from(">HH", data, o)
            if flag != 0x800 and flag != 0x008:
                return
            o += size + 2
        return True

    def decompress(self, data):
        """
        Decompress a record.

        Compressed records can have several chunks of compressed data.
        Note that the compression header uses a mix of big-endian and little numbers.

        each chunk has the following format:
            size  - big endian uint16, size of flag + crc + compdata
            flag  - big endian uint16 - always 0x800
            crc   - little endian uint32, crc32 of the decompressed data
        the final chunk has only 3 bytes: a zero size followed by a 2.

        the crc algorithm is the one labeled 'crc-32' on this page:
            http://crcmod.sourceforge.net/crcmod.predefined.html
        """
        result = b""
        o = 0
        while o < len(data) - 3:
            # note the mix of bigendian and little endian numbers here.
            size, flag = struct.unpack_from(">HH", data, o)
            storedcrc, = struct.unpack_from("<L", data, o + 4)

            C = zlib.decompressobj(-15)
            result += C.decompress(data[o + 8:o + 8 + size - 6])
            # note that we are not verifying the crc!

            o += size + 2
        return result
