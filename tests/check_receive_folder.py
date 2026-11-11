"""Run: python -m tests.check_receive_folder."""
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from lancollab.client.transfers import TransferManager


def main():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        sent = []
        transfer = TransferManager(lambda kind, payload: sent.append((kind, payload)), root / 'default')
        try:
            for number in range(2):
                tid = f'12345678-aaaa-bbbb-cccc-{number:012d}'
                transfer.on_offer({'transfer_id': tid, 'from': 'bob', 'name': 'empty.txt', 'size': 0, 'sha256': hashlib.sha256(b'').hexdigest()})
                destination = root / f'choice{number}'
                transfer.accept(tid, directory=destination)
                assert sent[-1] == ('FILE_ACCEPT', {'transfer_id': tid})
                transfer.on_end({'transfer_id': tid, 'total_chunks': 0})
                assert (destination / 'empty.txt').read_bytes() == b''
                assert not list(destination.glob('*.part'))
            assert transfer.downloads_dir == root / 'default'
            assert not (root / 'default').exists()
        finally:
            transfer.close()
    print('Per-transfer save directories passed; default unchanged; no partial files')


if __name__ == '__main__':
    main()
