# Bulk Certificate Generator

A Django + Django REST Framework API that takes **one request with many recipients**, validates each one, generates a PDF certificate for every valid recipient from a single predefined template, tracks progress per job and per recipient, and lets clients download the PDFs one at a time or as a ZIP.

**Stack:** Python 3.11, Django 5.2, DRF, SQLite, ReportLab (PDF).

---

## 1. Setup

```powershell
python -m venv .venv
.\.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
```

## 2. Run the application

```powershell
python manage.py runserver
```

```powershell
python manage.py runserver
```

- **Interactive Swagger UI:** Open [http://127.0.0.1:8000/api/docs/](http://127.0.0.1:8000/api/docs/) (or [http://127.0.0.1:8000/](http://127.0.0.1:8000/), which redirects there). You can test all endpoints directly with "Try it out".
- **Redoc Documentation:** [http://127.0.0.1:8000/api/redoc/](http://127.0.0.1:8000/api/redoc/)
- **OpenAPI Schema:** [http://127.0.0.1:8000/api/schema/](http://127.0.0.1:8000/api/schema/)
- **Live Certificate Preview:** [http://127.0.0.1:8000/api/template/preview/](http://127.0.0.1:8000/api/template/preview/) to immediately view the generated certificate template in your browser tab without creating a job.
- **Django Admin:** [http://127.0.0.1:8000/admin/](http://127.0.0.1:8000/admin/) (run `python manage.py createsuperuser` first).

Optional environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `CERTIFICATE_PROCESSING_MODE` | `thread` | `thread` = background processing, `sync` = generate inside the request |
| `CERTIFICATE_WORKER_THREADS` | `2` | Number of jobs processed in parallel |
| `CERTIFICATE_MAX_RECIPIENTS_PER_JOB` | `5000` | Maximum recipients in one request |
| `MEDIA_ROOT` | `./media` | Folder where generated PDFs are stored |

## 3. Run the tests

```powershell
python manage.py test
```

There are 46 tests in `certificates/tests/`. They cover job creation, request-level and recipient-level validation, PDF content, job status and progress (pending, partial, complete), isolation of a failure in one certificate (renderer error, storage error, whole job crashing), retry, retrieval (single PDF download and inline view, filtered and paginated list, ZIP), background scheduling, OpenAPI schema generation, and the template preview endpoint. Tests run jobs synchronously and write files to a temporary directory.

---

## 4. Submit a certificate generation request

`POST /api/jobs/`

```json
{
  "course_name": "Advanced Django & REST APIs",
  "issuer_name": "Acme Academy",
  "issue_date": "2026-10-01",
  "description": "Awarded for completing 40 hours of hands-on training.",
  "recipients": [
    {"name": "Alice Smith", "email": "alice@example.com"},
    {"name": "Bob Jones",   "email": "bob@example.com"},
    {"name": "Carol White", "email": "not-an-email"}
  ]
}
```

| Field | Rules |
|---|---|
| `course_name` | required, at most 200 characters |
| `issuer_name` | required, at most 100 characters |
| `issue_date` | optional, `YYYY-MM-DD`, defaults to today |
| `description` | optional, at most 200 characters |
| `recipients[]` | required, non-empty list, at most `CERTIFICATE_MAX_RECIPIENTS_PER_JOB` items |
| `recipients[].name` | required, at most 120 characters (trimmed) |
| `recipients[].email` | required, must be a valid email and unique within the job (case-insensitive) |

```bash
curl -X POST http://127.0.0.1:8000/api/jobs/ -H "Content-Type: application/json" -d @sample_request.json
# PowerShell:
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/jobs/ -ContentType application/json -Body (Get-Content sample_request.json -Raw)
```

The response is **`202 Accepted`** with the job object and a `Location` header. Poll that URL to follow progress.

Add `?strict=true` to reject the whole request if any recipient is invalid (see section 6).

## 5. Track progress and retrieve certificates

| Method & URL | Purpose |
|---|---|
| `GET  /api/docs/` (or `/api/swagger/`) | Interactive Swagger UI (test endpoints live) |
| `GET  /api/redoc/` | ReDoc API reference documentation |
| `GET  /api/schema/` | OpenAPI 3.0 specification |
| `GET  /api/template/preview/` | Live certificate preview (renders template directly with query params, e.g. `?name=Alice`) |
| `GET  /api/jobs/` | List jobs (paginated, ordered newest first) |
| `GET  /api/jobs/{job_id}/` | Job status, progress counts, and up to 100 failed/invalid recipients with reasons |
| `GET  /api/jobs/{job_id}/certificates/?status=success&page=1&page_size=100` | Per-recipient results, paginated. `status` can be `pending`, `success`, `failed` or `invalid` |
| `GET  /api/jobs/{job_id}/download/` | ZIP of every certificate generated so far |
| `POST /api/jobs/{job_id}/retry/` | Re-run certificates that `failed` |
| `GET  /api/certificates/{cert_id}/` | One certificate's status |
| `GET  /api/certificates/{cert_id}/download/` | Download the PDF (`?inline=true` to view in the browser tab) |

Example job status:

```json
{
  "id": "01504ca2-...",
  "status": "completed_with_errors",
  "total_recipients": 5,
  "progress": {"total": 5, "processed": 5, "pending": 0, "succeeded": 3,
               "failed": 0, "invalid": 2, "percent": 100.0},
  "links": {"self": ".../api/jobs/01504ca2-.../",
            "certificates": ".../api/jobs/01504ca2-.../certificates/",
            "download_zip": ".../api/jobs/01504ca2-.../download/"},
  "failures": [
    {"position": 2, "recipient_name": "Carol White", "recipient_email": "not-an-email",
     "status": "invalid", "error_message": "email: Enter a valid email address.", "download_url": null}
  ]
}
```

Each successful certificate in `/certificates/` has a `download_url`. `position` is the recipient's index in the submitted list, so the client can match results to its input.

**Job status values:** `pending` → `processing` → `completed` (all succeeded), `completed_with_errors` (some succeeded, some failed or were invalid), or `failed` (none succeeded).

**Certificate status values:** `pending`, `success`, `failed` (generation error, can be retried), `invalid` (rejected by validation; retrying won't help without corrected data).

---

## 6. Design decisions

### Processing model: background thread pool (asynchronous API)
- A request may contain thousands of recipients. Generating them inside the request would cause HTTP timeouts and hold a web worker for the whole time. So `POST` only **validates and stores** the job and its recipients (one transaction, using `bulk_create`), then returns `202` straight away.
- Generation runs in an **in-process `ThreadPoolExecutor`** (`certificates/tasks.py`). It is submitted with `transaction.on_commit` so the worker never starts before the job rows are committed.
- **Why not Celery?** Celery needs a broker (Redis or RabbitMQ) and a separate worker process. For this assignment, a zero-infrastructure setup that runs with `runserver` seemed better. The scheduler is isolated: the API calls `enqueue_job()` and nothing else, so moving to Celery or RQ only changes `tasks.py` (e.g. `process_job_task.delay(job_id)`). The processing logic in `services.py` is independent of how it is scheduled.
- **Trade-off:** the thread pool does not survive a process restart. Every certificate's state is stored in the database, and `process_job` only picks up `pending` certificates, so recovery is safe. Run `python manage.py process_pending_jobs` to finish any interrupted jobs. In production I would use Celery with retries and run multiple workers.
- `CERTIFICATE_PROCESSING_MODE=sync` runs jobs inside the request. Tests and debugging use it.

### Data model
- `CertificateJob`: the shared certificate details (course, issuer, date, description) plus the overall status and timestamps.
- `Certificate`: one row per **submitted** recipient, including invalid ones. It holds the original payload, `position`, `status`, `error_message`, the file and `generated_at`. One row per recipient is what lets the API report exactly which recipients succeeded or failed and why.
- Progress counts are not stored as counters. They are calculated from certificate statuses with a single annotated query (`CertificateJob.objects.with_counts()`), so they cannot drift out of sync. An `(job, status)` index keeps this query fast.

### Validation (two levels)
1. **Request level**, e.g. missing course name, bad date, `recipients` not a list or empty, too many recipients. These return `400` and nothing is created.
2. **Recipient level**, e.g. bad email, blank or too-long name, duplicate email, an item that is not an object, or characters the template can't render. Only that recipient is marked `invalid` with a readable reason. The valid recipients are still generated, which matches the requirement that one bad row shouldn't block the rest. If **every** recipient is invalid the request is rejected with `400`. Clients that want all-or-nothing behaviour can use `?strict=true`.

### Failure isolation
- Each certificate is generated in its own `try/except` and **committed on its own** (`services.generate_certificate`). An exception (renderer bug, disk full, ...) marks only that certificate as `failed` with `"<ExceptionType>: <message>"`, and the loop carries on.
- Committing per certificate also means progress is visible while a job runs, and a crash loses at most the certificate being worked on.
- If something outside per-certificate generation fails (e.g. the database), `run_job_safely` marks the job `failed` and its remaining certificates `failed` with the reason. A job therefore never stays stuck in `processing`.
- `POST /retry/` re-queues only `failed` certificates. Certificates that already succeeded are left untouched. A conditional `UPDATE` stops two retry calls from both starting the same job.

### Certificate template
- `certificates/pdf.py` holds the single template, drawn with ReportLab: landscape A4, a framed design, the recipient name, course, optional description, issue date, issuer, a seal and the certificate UUID (useful for verification). Nothing else in the code depends on the layout.
- Long names and course titles are shrunk to fit, then wrapped. They are never silently cut off.
- The built-in PDF fonts only cover Latin-1/cp1252. Text outside that set (e.g. Chinese) would come out as garbage, so it is **rejected during validation** with a clear message. *Fix if needed:* register a Unicode TTF font such as DejaVu Sans or Noto in `pdf.py` and remove the `unsupported_characters` check.

### Storage and retrieval
- PDFs are saved through Django's storage API to `MEDIA_ROOT/certificates/<job_id>/<cert_id>.pdf`. Switching to S3 is a configuration change (`django-storages`).
- Files are served through API endpoints rather than a public `/media/` URL. That gives one place to add authorisation later, plus friendly file names (`00000_alice-smith.pdf`).
- The ZIP is written to a temporary file instead of memory, so large jobs don't use up RAM.

### Database
- SQLite keeps setup to zero configuration. Because worker threads write while API requests read, it is configured with WAL mode, a 20 s busy timeout and `IMMEDIATE` transactions (`config/settings.py`). Each job runs its certificates one after another, and parallelism is capped by `CERTIFICATE_WORKER_THREADS`, which limits concurrent writers. For production, use PostgreSQL by changing only `DATABASES`.

### Out of scope / possible next steps
- Authentication and per-owner job visibility (endpoints are open right now).
- Celery and Redis for durable, horizontally scalable workers.
- CSV upload, email delivery, webhook on job completion, cleanup/expiry of old files, and a public certificate-verification endpoint using the certificate ID.

---

## Project layout

```
config/                      Django project (settings, urls)
certificates/
  models.py                  CertificateJob, Certificate
  serializers.py             input validation + output shapes
  views.py                   API endpoints
  services.py                generation logic (per-certificate isolation, finalisation)
  tasks.py                   scheduling: thread pool / sync (swap point for Celery)
  pdf.py                     the predefined certificate template (ReportLab)
  management/commands/process_pending_jobs.py   recovery after restarts
  tests/                     test suite
sample_request.json          example payload (includes 2 invalid recipients)
```
