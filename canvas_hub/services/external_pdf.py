"""Fixed PDF text worker: isolated process, no page scripts, no file paths.

The parent terminates this process after 12 seconds. Linux additionally bounds
address space/CPU; all platforms cap input, pages and emitted text. Scans need a
connected agent; this reader never launches OCR or external programs.
"""
import sys
from io import BytesIO


def main():
    if sys.platform == 'linux':
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    from pdfminer.high_level import extract_text_to_fp

    class BoundedText:
        def __init__(self):
            self.remaining = 200_000

        def write(self, text):
            if isinstance(text, bytes):
                text = text.decode('utf-8', errors='replace')
            sys.stdout.write(text[:self.remaining])
            self.remaining -= len(text)
            if self.remaining <= 0:
                raise SystemExit(0)

    raw = sys.stdin.buffer.read(1_000_001)
    if len(raw) > 1_000_000:
        raise ValueError('PDF too large')
    extract_text_to_fp(BytesIO(raw), BoundedText(), maxpages=20, output_type='text', codec=None)


if __name__ == '__main__':
    main()
