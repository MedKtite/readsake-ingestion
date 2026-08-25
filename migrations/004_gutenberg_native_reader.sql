ALTER TABLE books ADD COLUMN IF NOT EXISTS gutenberg_id INT;
UPDATE books b SET gutenberg_id = g.gutenberg_id FROM gutenberg_catalog g WHERE b.format = 'physical' AND b.gutenberg_id IS NULL AND lower(b.title) = lower(g.title);
UPDATE books b SET reading_file_key = g.reader_file_key FROM gutenberg_catalog g WHERE b.format = 'physical' AND b.gutenberg_id = g.gutenberg_id AND g.reader_file_key IS NOT NULL AND (b.reading_file_key IS NULL OR b.reading_file_key = '');
