"""Archive contents must retain bytes without allowing filesystem extraction."""
from io import BytesIO
import gzip
from pathlib import PurePosixPath
import stat
import struct
import tarfile
import unittest
import warnings
import zipfile

from gmss.uploads import archive_files, MAX_FILE_BYTES, MAX_UPLOAD_BYTES, MAX_MEMBERS


def zip_bytes(files):
    output = BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in files:
            archive.writestr(name, content)
    return output.getvalue()


class ArchiveTests(unittest.TestCase):
    def test_zip_and_tar_keep_chinese_names_nested_paths_and_original_bytes(self):
        files = [('石家庄/13点.dat', b'original\x00\xff\r\n'),
                 ('石家庄/各10kV馈线/线路.xls', b'excel\xff'), ('石家庄/readme.pdf', b'ignored')]
        archives = [('dataset.zip', zip_bytes(files))]
        for suffix, mode in (('.tar', 'w'), ('.tar.gz', 'w:gz'), ('.tgz', 'w:gz')):
            output = BytesIO()
            with tarfile.open(fileobj=output, mode=mode) as archive:
                for name, content in files:
                    member = tarfile.TarInfo('./'+name);member.size = len(content)
                    archive.addfile(member, BytesIO(content))
            archives.append(('dataset'+suffix, output.getvalue()))
        for name, data in archives:
            with self.subTest(name=name):
                actual = archive_files(name, data)
                self.assertEqual(actual, [(PurePosixPath(p), content) for p, content in files[:2]])

    def test_loose_files_are_wrapped_as_one_dataset(self):
        files = [('13.dat', b'dat'), ('feeders/a.xls', b'excel')]
        self.assertEqual([str(p) for p, _ in archive_files('my-case.zip', zip_bytes(files))],
                         ['my-case/13.dat', 'my-case/feeders/a.xls'])

    def test_legacy_gbk_zip_names_are_decoded(self):
        # Replace an equal-length ASCII filename in both ZIP headers. No UTF-8 flag.
        encoded = '13点.dat'.encode('gbk')
        data = zip_bytes([('a'*len(encoded), b'bytes')]).replace(b'a'*len(encoded), encoded)
        self.assertEqual(str(archive_files('dataset.zip', data)[0][0]), 'dataset/13点.dat')

    def test_rejects_traversal_links_duplicates_encryption_and_invalid_archives(self):
        cases = [(f'{n}.zip', zip_bytes([(p, b'bad')])) for n, p in enumerate(
            ('../outside.dat', '/absolute.dat', 'C:\\drive.dat', 'folder/../../escape.dat'))]
        link = zipfile.ZipInfo('link.dat');link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        cases.append(('link.zip', zip_bytes([(link, b'/etc/passwd')])))
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            cases.append(('duplicate.zip', zip_bytes([('same.dat', b'a'), ('same.dat', b'b')])))
        data = bytearray(zip_bytes([('13.dat', b'dat')]))
        central = data.index(b'PK\x01\x02')
        struct.pack_into('<H', data, central+8, 1)
        cases.extend([('encrypted.zip', bytes(data)), ('broken.zip', b'not a zip'),
                      ('broken.tar.gz', b'not a tar'), ('missing.zip', zip_bytes([('a.xls', b'a')])),
                      ('file.rar', b'rar')])
        corrupt=bytearray(zip_bytes([('13.dat', b'sample content')]))
        corrupt[30+len('13.dat')]=0xff
        cases.append(('corrupt-deflate.zip',bytes(corrupt)))
        output = BytesIO()
        with tarfile.open(fileobj=output, mode='w') as archive:
            member = tarfile.TarInfo('link.dat');member.type = tarfile.SYMTYPE;member.linkname = '../outside'
            archive.addfile(member)
        cases.append(('link.tar', output.getvalue()))
        for name, data in cases:
            with self.subTest(name=name), self.assertRaises(ValueError):
                archive_files(name, data)

    def test_limits_are_checked_before_decompression(self):
        for size in (MAX_FILE_BYTES+1, MAX_UPLOAD_BYTES+1):
            data = bytearray(zip_bytes([('13.dat', b'small')]))
            central = data.index(b'PK\x01\x02')
            struct.pack_into('<I', data, central+24, size)
            with self.subTest(size=size), self.assertRaises(ValueError):
                archive_files('oversized.zip', data)
        with self.assertRaises(ValueError):
            archive_files('many.zip', zip_bytes([(f'{n}.dat', b'') for n in range(501)]))
        with self.assertRaises(ValueError):
            archive_files('large-metadata.tar.gz', gzip.compress(b'\0'*(MAX_UPLOAD_BYTES+MAX_MEMBERS*1024+1)))


if __name__ == '__main__':
    unittest.main()
