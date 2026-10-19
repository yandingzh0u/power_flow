"""Bounded archive reading; never extract archive paths onto the filesystem."""
from io import BytesIO
import gzip
from pathlib import PurePosixPath
import stat
import tarfile
import zipfile
import zlib

MAX_UPLOAD_BYTES = 32 * 1024**2
MAX_FILE_BYTES = 16 * 1024**2
MAX_FILES = 500
MAX_MEMBERS = 2000
ARCHIVE_SUFFIXES = ('.tar.gz', '.tgz', '.tar', '.zip')
DATA_SUFFIXES = ('.dat', '.xls', '.xlsx')


def archive_files(name, data):
    """Return original DAT/Excel bytes under one dataset root, with size caps."""
    if not isinstance(name, str) or not name or any(c in name for c in ('/', '\\', ':', '\0')) or name.startswith('.'):
        raise ValueError('压缩包文件名无效')
    suffix = next((s for s in ARCHIVE_SUFFIXES if name.lower().endswith(s)), None)
    if suffix is None:
        raise ValueError('支持 ZIP、TAR、TAR.GZ 和 TGZ 压缩包')
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise ValueError('压缩包为空或超过 32 MiB')
    files, seen, expanded, members = [], set(), 0, 0

    def accept(raw_name, size, regular, directory, reader):
        nonlocal expanded, members
        members += 1
        if members > MAX_MEMBERS:
            raise ValueError('压缩包条目超过 2000 个')
        raw_name = raw_name.replace('\\', '/')
        if raw_name.startswith('/') or '\0' in raw_name or ':' in raw_name or '..' in raw_name.split('/'):
            raise ValueError('压缩包包含不安全的文件路径')
        path = PurePosixPath(raw_name)
        if directory:
            return
        if not regular:
            raise ValueError('压缩包不支持符号链接、硬链接或特殊文件')
        expanded += size
        if expanded > MAX_UPLOAD_BYTES or size < 0:
            raise ValueError('压缩包解压后总大小限 32 MiB')
        if any(p.startswith('.') or p == '__MACOSX' for p in path.parts) or path.suffix.lower() not in DATA_SUFFIXES:
            return
        if str(path) in seen:
            raise ValueError('压缩包中存在重复文件路径')
        seen.add(str(path))
        if len(seen) > MAX_FILES or size > MAX_FILE_BYTES:
            raise ValueError('最多导入 500 个数据文件，单文件限 16 MiB')
        with reader() as source:
            content = source.read(MAX_FILE_BYTES + 1)
        if len(content) != size:
            raise ValueError('压缩包文件大小异常或超过单文件限制')
        files.append((path, content))

    try:
        if suffix == '.zip':
            # UTF-8 flags take precedence. Legacy Chinese ZIPs commonly use GBK.
            try:
                archive = zipfile.ZipFile(BytesIO(data), metadata_encoding='gb18030')
            except UnicodeDecodeError:
                archive = zipfile.ZipFile(BytesIO(data))
            with archive:
                for member in archive.infolist():
                    if member.flag_bits & 1:
                        raise ValueError('不支持加密压缩包，请先解密后上传')
                    mode = stat.S_IFMT(member.external_attr >> 16)
                    accept(member.filename, member.file_size, mode in (0, stat.S_IFREG), member.is_dir(),
                           lambda m=member: archive.open(m))
        else:
            stream = BytesIO(data)
            if suffix in ('.tar.gz', '.tgz'):
                # Bound the TAR stream before its PAX/GNU metadata is parsed too.
                limit = MAX_UPLOAD_BYTES + MAX_MEMBERS * 1024
                with gzip.GzipFile(fileobj=stream) as compressed:
                    unpacked = compressed.read(limit + 1)
                if len(unpacked) > limit:
                    raise ValueError('压缩包展开后超过大小限制')
                stream = BytesIO(unpacked)
            with tarfile.open(fileobj=stream, mode='r:') as archive:
                for member in archive:
                    accept(member.name, member.size, member.isfile(), member.isdir(),
                           lambda m=member: archive.extractfile(m))
    except (zipfile.BadZipFile, tarfile.TarError, EOFError, OSError, RuntimeError, NotImplementedError, zlib.error) as error:
        raise ValueError('压缩包损坏、被加密或使用了不支持的压缩方式') from error
    if not any(p.suffix.lower() == '.dat' for p, _ in files):
        raise ValueError('压缩包中没有 DAT 运行断面')
    roots = {p.parts[0] for p, _ in files}
    if len(roots) != 1 or any(len(p.parts) == 1 for p, _ in files):
        root = name[:-len(suffix)]
        if not root or root.startswith('.'):
            raise ValueError('压缩包数据集名称无效')
        files = [(PurePosixPath(root) / p, content) for p, content in files]
    return files
