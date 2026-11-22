"""Compile the pinned source at image build time, outside the charged run."""
import compileall
from pathlib import Path
import py_compile
import tempfile
import zipfile

archive_path = Path(__file__).with_name('baseline.zip')
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(root)
    compileall.compile_dir(root, quiet=1, invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)
    output = archive_path.with_suffix('.compiled.zip')
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob('*')):
            if path.is_file():
                archive.write(path, path.relative_to(root))
    output.replace(archive_path)
