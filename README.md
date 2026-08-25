# Backend — 834 EDI Converter

Django 5.2 + DRF. Serves JSON only; the front end is a separate project.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py bootstrap_users
python manage.py seed_segment_elements
python manage.py runserver
```

Read the note on `DJANGO_SECRET_KEY` in `.env.example` before your first upload.
It is the pepper for SSN identity matching, not just a session key.

## Apps

`files` holds the uploaded 834 and the generated workbook as paths plus
metadata; nothing binary goes in the database. `mapping` holds the versioned
column rules and the X12 dictionary behind the dropdowns. `conversion` is the
append-only audit trail of runs. `members` is the people, their coverage spans
and their appearance in each day's file. `edi` is the API surface and the
services that do the parsing, validating, syncing and workbook generation.
`users` is session auth.

## Services

`edi/services/parser.py` streams the file into `Segment` objects that keep their
elements together, so qualifier-based mapping is possible and INS loop
boundaries are findable. `loop_extractor.py` groups segments into member loops.
`x12_834_to_db.py` turns one loop into a field dict, including one coverage
entry per HD segment. `member_sync.py` reconciles that dict against the stored
member and spans. `ingest.py` drives the whole thing for one uploaded file and
returns a summary. `excel_generator.py` writes the workbook a row at a time.

## Testing

```bash
python manage.py test
python manage.py check --deploy
```
