# Software Requirements Specification (SRS): STS Pulse

## 1. Introduction

### 1.1 Purpose
This Software Requirements Specification (SRS) details the functional and non-functional requirements for **STS Pulse**, a unified School Learning Management System (LMS) and Human Resources (HR) portal for STS Group. It outlines the core question generation workflow, academic knowledge repository, employee lifecycle, attendance, and leave tracking.

### 1.2 System Scope
STS Pulse combines two primary workspaces served through a customized Django administration interface:
- **Academic Workspace (`questionbank`)**: Curriculum-aligned question authoring, automated Claude/Gemini generation, blueprint verification, confidential final paper preparation, and DOCX/PDF exports.
- **HR Workspace (`hr`)**: Employee registry, role/department hierarchies, daily punch in/out attendance, leave applications with approval routing, and monthly operational reporting.
- **Shared Core Services**: User profile management, role-based access control, in-app/email/Make.com notifications, and tamper-resistant audit logging.

---

## 2. Overall Description

### 2.1 Product Perspective
STS Pulse is a self-hosted monolithic web application developed in Python (Django 6.1) that runs on Windows 11 with support for both MySQL 8.4+ and SQLite. It replaces fragmented spreadsheet-based question management and manual leave requests.

### 2.2 User Classes and Characteristics
1. **Super Admin**: System owner with complete control over users, permissions, subject assignments, and system audit logs.
2. **Teacher / Subject Expert**: Prepares learning materials, drafts curriculum questions, and authors exam blueprints.
3. **Head of Department (HOD)**: Academic reviewer responsible for approving questions and blueprints for assigned subjects, as well as approving team leave.
4. **Exam Controller**: Sole custodian of confidential final exam papers. Generates, edits, locks, and exports final examination papers across all subjects.
5. **Read-Only Academic User**: Observational role with access to view materials, approved questions, and academic calendars.
6. **HR Manager**: Manages employee records, departments, designations, company holidays, and organization-wide attendance/leave reports.
7. **Employee**: Standard staff member with self-service punch in/out, profile viewing, and leave request submission.

---

## 3. Functional Requirements

### 3.1 Academic Workspace (`questionbank`)

#### 3.1.1 Knowledge Base Management
- **FR-KB-01**: System shall accept knowledge files in PDF, DOCX, TXT, and MD formats up to 20MB.
- **FR-KB-02**: System shall automatically extract and index full text upon document upload (`content_text`).
- **FR-KB-03**: Knowledge sources must be tagged with Subject, Grade, Material Type (Textbook, Syllabus, Lesson Plan, Sample Questions, Notes), Chapter, and Topic.

#### 3.1.2 Practice & Bank Question Management
- **FR-QB-01**: Teachers shall generate draft questions via an AI generation form selecting active knowledge sources, target difficulty, question type, and count.
- **FR-QB-02**: System shall automatically detect duplicate or near-identical questions using fuzzy matching and sample question comparison.
- **FR-QB-03**: Question approval workflow shall enforce strict "no self-approval" (HOD cannot approve their own drafted questions).

#### 3.1.3 Blueprint Specification & Lifecycle
- **FR-BP-01**: Teachers shall create exam blueprints with structured sections, item counts, marks per item, question types, and difficulty distribution.
- **FR-BP-02**: Blueprints must progress through a formal state machine: `DRAFT` -> `IN_REVIEW` -> `APPROVED` (or `RETURNED`) -> `FINALIZED`.

#### 3.1.4 Confidential Final Exam Papers
- **FR-FP-01**: Only users with the `manage_final_papers` permission (Exam Controller role and Superusers) can access final paper records.
- **FR-FP-02**: Generation of final papers must execute asynchronously in background threads with a 15-minute timeout guard to prevent worker locking.
- **FR-FP-03**: Generation process must strictly match question counts, section marks, and structure defined in the approved blueprint.
- **FR-FP-04**: An automated AI review pass shall validate syllabus compliance and answer correctness.
- **FR-FP-05**: Once finalized, exam controllers can lock papers. Locked papers cannot be regenerated or mutated.
- **FR-FP-06**: System shall export locked final papers to formatted PDF and DOCX documents with optional answer keys.

---

### 3.2 HR Workspace (`hr`)

#### 3.2.1 Employee Records & Hierarchy
- **FR-HR-01**: Maintain structured employee records including Department, Designation, Line Manager (`reports_to`), Contact details, and Joining Date.
- **FR-HR-02**: Enforce department head visibility over direct team members.

#### 3.2.2 Attendance Tracking
- **FR-AT-01**: Provide interactive web punch in/out interface.
- **FR-AT-02**: Automatically compute status (`Present`, `Late`, `Half Day`, `Absent`, `On Leave`) and working hours based on holiday calendars.
- **FR-AT-03**: Export monthly summary reports to CSV.

#### 3.2.3 Leave Management
- **FR-LV-01**: Support configurable paid and unpaid leave types with annual quota allocations.
- **FR-LV-02**: Enforce multi-tier approval where line managers or department heads review requests, preventing self-approval.

---

### 3.3 Core & Shared Services

#### 3.3.1 Workspace Routing
- **FR-WS-01**: Upon login, users are routed to their permitted workspace or a workspace selector if eligible for both.
- **FR-WS-02**: Dynamic sidebar filters admin apps according to active workspace session.

#### 3.3.2 Notifications & Audit Log
- **FR-NT-01**: System shall dispatch in-app notifications with optional deduplication keys.
- **FR-NT-02**: Webhook events shall be asynchronously dispatched to Make.com and standard SMTP mailer.
- **FR-AU-01**: Immutable audit logs must capture all administrative, approval, generation, and export actions. Final paper text and passwords must never be stored in audit logs.

---

## 4. Non-Functional Requirements

### 4.1 Security & Confidentiality
- **NFR-SEC-01**: Final papers and their generated questions must remain completely confidential and filtered out from generic API responses, notifications, and non-privileged views.
- **NFR-SEC-02**: Session cookies must be secure, HttpOnly, and support optional "Remember Me" expiration policies.
- **NFR-SEC-03**: Media files must only be accessible to authenticated staff members (`staff_member_required`).

### 4.2 Reliability & Performance
- **NFR-PERF-01**: Long-running AI operations must never block the main web thread or HTTP request-response cycle.
- **NFR-PERF-02**: Database queries for academic resources must respect subject assignment scoping via optimized indexed joins.

### 4.3 Internationalization & Localization
- **NFR-I18N-01**: All timestamps and date computations must be localized to Bangladesh Standard Time (`Asia/Dhaka`).
- **NFR-I18N-02**: Content storage and AI prompts must fully support UTF-8 (Bangla and English text).

