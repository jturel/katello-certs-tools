#
# Copyright 2013 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public License,
# version 2 (GPLv2). There is NO WARRANTY for this software, express or
# implied, including the implied warranties of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. You should have received a copy of GPLv2
# along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.
#
# Red Hat trademarks are not licensed under GPLv2. No permission is
# granted to use or replicate Red Hat trademarks that are incorporated
# in this software or its documentation.
#

from __future__ import print_function
import os
import sys
import rpm
import struct
import functools

# Expose a bunch of useful constants from rpm
error = rpm.error

# need this for rpm-pyhon < 4.6 (e.g. on RHEL5)
rpm.RPMTAG_FILEDIGESTALGO = 5011

# these values are taken from /usr/include/rpm/rpmpgp.h
# PGPHASHALGO_MD5             =  1,   /*!< MD5 */
# PGPHASHALGO_SHA1            =  2,   /*!< SHA1 */
# PGPHASHALGO_RIPEMD160       =  3,   /*!< RIPEMD160 */
# PGPHASHALGO_MD2             =  5,   /*!< MD2 */
# PGPHASHALGO_TIGER192        =  6,   /*!< TIGER192 */
# PGPHASHALGO_HAVAL_5_160     =  7,   /*!< HAVAL-5-160 */
# PGPHASHALGO_SHA256          =  8,   /*!< SHA256 */
# PGPHASHALGO_SHA384          =  9,   /*!< SHA384 */
# PGPHASHALGO_SHA512          = 10,   /*!< SHA512 */
PGPHASHALGO = {
  1: 'md5',
  2: 'sha1',
  3: 'ripemd160',
  5: 'md2',
  6: 'tiger192',
  7: 'haval-5-160',
  8: 'sha256',
  9: 'sha384',
  10: 'sha512'
}


class InvalidPackageError(Exception):
    pass


class RPM_Header:
    "Wrapper class for an rpm header - we need to store a flag is_source"
    def __init__(self, hdr, is_source=None):
        self.hdr = hdr
        self.is_source = is_source
        self.packaging = 'rpm'
        self.signatures = []
        self._extract_signatures()

    def __getitem__(self, name):
        return self.hdr[name]

    def __getattr__(self, name):
        return getattr(self.hdr, name)

    def __nonzero__(self):
        if self.hdr:
            return True
        else:
            return False

    def checksum_type(self):
        if self.hdr[rpm.RPMTAG_FILEDIGESTALGO] \
           and self.hdr[rpm.RPMTAG_FILEDIGESTALGO].decode('utf-8') in PGPHASHALGO:
            checksum_type = PGPHASHALGO[self.hdr[rpm.RPMTAG_FILEDIGESTALGO]]
        else:
            checksum_type = 'md5'
        return checksum_type

    def is_signed(self):
        if hasattr(rpm, "RPMTAG_DSAHEADER"):
            dsaheader = self.hdr["dsaheader"]
        else:
            dsaheader = 0
        if self.hdr["siggpg"] or self.hdr["sigpgp"] or dsaheader:
            return 1
        return 0

    def _extract_signatures(self):
        header_tags = [
            [rpm.RPMTAG_DSAHEADER, "dsa"],
            [rpm.RPMTAG_RSAHEADER, "rsa"],
            [rpm.RPMTAG_SIGGPG, "gpg"],
            [rpm.RPMTAG_SIGPGP, 'pgp'],
        ]
        for ht, sig_type in header_tags:
            ret = self.hdr[ht]
            if not ret:
                continue
            ret_len = len(ret)
            if ret_len < 17:
                continue
            # Get the key id - hopefully we get it right
            elif ret_len <= 65:  # V3 DSA signature
                key_id = ret[9:17]
            elif ret_len <= 72:  # V4 DSA signature
                key_id = ret[18:26]
            elif ret_len <= 536:  # V3 RSA/SHA256 signature
                key_id = ret[10:18]
            else:  # V4 RSA/SHA signature
                key_id = ret[19:27]

            key_id_len = len(key_id)
            key_format = "%dB" % key_id_len
            t = struct.unpack(key_format, key_id)
            key_format = "%02x" * key_id_len
            key_id = key_format % t
            self.signatures.append({
                'signature_type': sig_type,
                'key_id': key_id,
                'signature': ret
            })


def get_header_byte_range(package_file):
    """
    Return the start and end bytes of the rpm header object.

    For details of the rpm file format, see:
    http://www.rpm.org/max-rpm/s1-rpm-file-format-rpm-file-format.html
    """

    lead_size = 96

    # Move past the rpm lead
    package_file.seek(lead_size)

    sig_size = get_header_struct_size(package_file)

    # Now we can find the start of the actual header.
    header_start = lead_size + sig_size

    package_file.seek(header_start)

    header_size = get_header_struct_size(package_file)

    header_end = header_start + header_size

    return (header_start, header_end)


def get_header_struct_size(package_file):
    """
    Compute the size in bytes of the rpm header struct starting at the current
    position in package_file.
    """
    # Move past the header preamble
    package_file.seek(8, 1)

    # Read the number of index entries
    header_index = package_file.read(4)
    (header_index_value, ) = struct.unpack('>I', header_index)

    # Read the the size of the header data store
    header_store = package_file.read(4)
    (header_store_value, ) = struct.unpack('>I', header_store)

    # The total size of the header. Each index entry is 16 bytes long.
    header_size = 8 + 4 + 4 + header_index_value * 16 + header_store_value

    # Headers end on an 8-byte boundary. Round out the extra data.
    round_out = header_size % 8
    if round_out != 0:
        header_size = header_size + (8 - round_out)

    return header_size


SHARED_TS = None


def get_package_header(filename=None, file_stream=None, fd=None):
    """ Loads the package header from a file / stream / file descriptor
        Raises rpm.error if an error is found, or InvalidPacageError if package is
        busted
    """
    global SHARED_TS
    # XXX Deal with exceptions better
    if (filename is None and file_stream is None and fd is None):
        raise ValueError("No parameters passed")

    if filename is not None:
        f = open(filename)
    elif file_stream is not None:
        f = file_stream
        f.seek(0, 0)
    else:  # fd is not None
        f = None

    if f is None:
        os.lseek(fd, 0, 0)
        file_desc = fd
    else:
        file_desc = f.fileno()

    # don't try to use rpm.readHeaderFromFD() here, it brokes signatures
    # see commit message
    if not SHARED_TS:
        SHARED_TS = rpm.ts()
    SHARED_TS.setVSFlags(-1)

    rpm.addMacro('_dbpath', '/var/cache/rhn/rhnpush-rpmdb')
    try:
        hdr = SHARED_TS.hdrFromFdno(file_desc)
        rpm.delMacro('_dbpath')
    except RuntimeError:
        rpm.delMacro('_dbpath')
        raise

    if hdr is None:
        raise InvalidPackageError
    is_source = hdr[rpm.RPMTAG_SOURCEPACKAGE]

    return RPM_Header(hdr, is_source)


class MatchIterator:
    def __init__(self, tag_name=None, value=None):
        # Query by name, by default
        if not tag_name:
            tag_name = "name"

        # rpm 4.1 or later
        self.ts = rpm.TransactionSet()
        self.ts.setVSFlags(8)

        m_args = (tag_name,)
        if value:
            m_args += (value,)
        # pylint: disable=E1101
        self.mi = self.ts.dbMatch(*m_args)

    def pattern(self, tag_name, mode, pattern):
        self.mi.pattern(tag_name, mode, pattern)

    def next(self):
        try:
            hdr = self.mi.next()
        except StopIteration:
            hdr = None

        if hdr is None:
            return None
        is_source = hdr[rpm.RPMTAG_SOURCEPACKAGE]
        return RPM_Header(hdr, is_source)


def headerLoad(data):
    hdr = rpm.headerLoad(data)
    is_source = hdr[rpm.RPMTAG_SOURCEPACKAGE]
    return RPM_Header(hdr, is_source)


def labelCompare(t1, t2):
    return rpm.labelCompare(t1, t2)


def nvre_compare(t1, t2):
    def build_evr(p):
        evr = [p[3], p[1], p[2]]
        evr = map(str, evr)
        if evr[0] == "":
            evr[0] = None
        return evr
    if t1[0] != t2[0]:
        raise ValueError("You should only compare packages with the same name")
    evr1, evr2 = (build_evr(t1), build_evr(t2))
    return rpm.labelCompare(evr1, evr2)


def hdrLabelCompare(hdr1, hdr2):
    """ take two RPMs or headers and compare them for order """

    if hdr1['name'] == hdr2['name']:
        hdr1 = [hdr1['epoch'], hdr1['version'].decode('utf-8'), hdr1['release'].decode('utf-8')]
        hdr2 = [hdr2['epoch'], hdr2['version'].decode('utf-8'), hdr2['release'].decode('utf-8')]
        if hdr1[0]:
            hdr1[0] = str(hdr1[0])
        if hdr2[0]:
            hdr2[0] = str(hdr2[0])
        return rpm.labelCompare(hdr1, hdr2)
    elif hdr1['name'] < hdr2['name']:
        return -1
    return 1


hdrLabelCompareKey = functools.cmp_to_key(hdrLabelCompare)


def sortRPMs(rpms):
    """ Sorts a list of RPM files. They *must* exist.  """

    assert isinstance(rpms, type([]))
    return sorted(rpms, key=lambda rpm: hdrLabelCompareKey(get_package_header(rpm)))


def getInstalledHeader(rpmName):
    """ quieries the RPM DB for a header matching rpmName. """

    hdr = None
    ts = rpm.TransactionSet()
    mi = ts.dbMatch()
    mi.pattern("name", rpm.RPMMIRE_STRCMP, rpmName)
    for h in mi:
        hdr = h
    return hdr


if __name__ == '__main__':
    app_mi = MatchIterator("name")
    app_mi.pattern("name", rpm.RPMMIRE_GLOB, "*ker*")
    while 1:
        h = app_mi.next()
        if not h:
            break
        print(h['name'])
    sys.exit(1)
    app_hdr1 = get_package_header(filename="/tmp/python-1.5.2-42.72.i386.rpm")
    print(dir(app_hdr1))
    # Sources
    app_hdr1 = get_package_header(filename="/tmp/python-1.5.2-42.72.src.rpm")
    app_hdr2 = headerLoad(app_hdr1.unload())
    print(app_hdr2)
    print(len(app_hdr2.keys()))
