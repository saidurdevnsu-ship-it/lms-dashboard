# Technology Stack Specification: STS Pulse

## 1. Core Runtime & Architecture

| Component | Technology | Version | Purpose |
|---|---|---|---|
| **Programming Language** | Python | 3.15.x | Core application programming language |
| **Web Framework** | Django | 6.1.2 | High-level MVC web framework & admin UI |
| **Operating System** | Microsoft Windows | Windows 11 | Host and runtime development environment |
| **Virtual Environment** | venv | Standard Python venv | Dependency isolation at `venv/` |
| **Primary Database** | MySQL | 8.4 LTS | Production database with `utf8mb4_0900_ai_ci` collation |
| **Development Fallback DB** | SQLite | 3.x | Zero-config local development and testing |
| **Timezone** | IANA Zoneinfo | `Asia/Dhaka` (UTC+6) | School operations and attendance scheduling |

---

## 2. Python Packages & Dependencies

Key libraries specified in `requirements.txt`:

```text
django>=6.1,<6.2           # Web framework core
mysqlclient>=2.2.0         # Fast C-based MySQL database connector
anthropic>=0.40.0          # Anthropic Claude SDK for AI generation
google-genai>=0.1.0        # Google Gemini SDK for secondary AI provider
pymupdf>=1.24.0            # High-performance PDF text extraction (fitz)
python-docx>=1.1.0         # Word DOCX parsing and exam export generation
weasyprint>=62.0           # HTML-to-PDF rendering engine for locked papers
requests>=2.32.0           # HTTP requests for Make.com webhook dispatches
openpyxl>=3.1.0            # Spreadsheet generation for attendance reporting
python-dotenv>=1.0.0       # Local environment configuration loading
```

---

## 3. Storage & Document Processing Engines

### 3.1 Document Ingestion & Text Extraction
- **PyMuPDF (`fitz`)**: Reads syllabus and textbook PDFs, extracts clean body text, and discards unneeded raster graphics.
- **`python-docx`**: Parses uploaded Word documents (`.docx`) to extract chapter structures, bullet points, and sample questions into `KnowledgeSource.content_text`.
- **Text & Markdown**: Read directly via native Python UTF-8 streams with BOM handling.

### 3.2 Document Rendering & Export
- **PDF Generation**: Powered by WeasyPrint using standard HTML/CSS templates (`templates/questionbank/final_export.html` and `static/css/print-doc.css`).
- **DOCX Generation**: Native XML document construction via `python-docx` using standardized STS school header typography, question layout tables, and answer sheets.

---

## 4. AI Provider Integration Layer

STS Pulse features a unified, pluggable LLM interface located at `questionbank/llm.py` and `questionbank/ai.py`:

```
                 +-----------------------------+
                 |    questionbank.ai module   |
                 +--------------+--------------+
                                |
                                v
                 +-----------------------------+
                 |   questionbank.llm.ask()    |
                 +--------------+--------------+
                                |
               +----------------+----------------+
               |                                 |
               v                                 v
     +--------------------+            +--------------------+
     |  Anthropic Claude  |            |   Google Gemini    |
     | (Claude 3.5 / 3.7) |            |  (Gemini 2.5 Pro)  |
     +--------------------+            +--------------------+
```

- **Anthropic Claude Engine**: Default provider; streams structured JSON via system prompts with prompt caching (`cache_control`).
- **Google Gemini Engine**: Secondary provider enabled via `AI_PROVIDER=gemini`; translates requests to `google-genai` contents and parts.
- **Execution Model**: Asynchronous non-blocking background workers managed using Python `threading.Thread` with DB connection teardown guards.

---

## 5. Webhook, Mail, and Automation Services

- **Make.com (Integromat)**: Asynchronous event delivery for school management workflows (`notifications.make.send_event`). Events are queued and emitted following database `transaction.on_commit`.
- **Django Mail Service**: SMTP backend (`notifications.mail.send_email`) generating transactional HTML and plain-text emails.
- **Audit Pipeline**: Thread-safe database logging utilizing Django `post_save` and `request_finished` signals.

---

## 6. Frontend & UI Stack

STS Pulse operates exclusively within an enhanced Django Admin framework:
- **Styling**: Custom CSS stylesheets (`admin-base.css`, `admin-dashboard.css`, `punch.css`, `print-doc.css`).
- **Templating**: Django Template Language (DTL) overriding `templates/admin/`.
- **Interactivity**: Lightweight vanilla JavaScript for punch in/out clocks, attendance status selectors, and dynamic blueprint forms.

