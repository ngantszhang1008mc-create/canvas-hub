-- Optional OCR: mean word confidence (0-100) for text read from images and scanned PDFs.
ALTER TABLE resources ADD COLUMN ocr_confidence REAL;
