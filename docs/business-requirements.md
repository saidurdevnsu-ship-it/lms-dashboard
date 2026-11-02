# Business requirements

Condensed from the product spec `C:\Users\User\Desktop\Plan.docx` ("Plan - spec for development: phase 1") and the
current implementation plan in `.hermes/plans/`. When the spec and the decisions below disagree, the decisions win.

## Goal

An AI question-paper generator (Django + Claude API) that writes curriculum-aligned papers from uploaded textbooks,
syllabi, teaching materials and sample questions, with human review as the final quality control; plus a basic HR
module in the same application. Spec stack: Django, MySQL, PyMuPDF, python-docx, PDF/DOCX export. (The spec's
Bootstrap frontend was not built: the UI is Django admin only.)

## Features (spec)

- **A. Knowledge base**: upload and organise textbooks, syllabi, lesson plans and sample questions by subject, grade,
  chapter and topic; extract and store text; add, update, remove materials.
- **B. AI generation**: choose subject, grade, chapters and a sample paper; configure question types, difficulty,
  total marks and distribution; generate questions with Claude from the selected curriculum.
- **C. Validation and review**: validate total marks, question count and distribution; check syllabus alignment,
  duplication and factual consistency; review, edit, regenerate and approve.
- **D. Export**: preview, export approved papers to PDF and DOCX, save papers for reuse.
- **User management**: secure login/logout, password reset, profile management; admin-controlled user creation,
  activation, deactivation and role assignment; server-side role-based access to pages, actions and data; audit log
  of generation, editing, approval and export. Final paper approval restricted to authorised reviewers.

## Roles and permissions

| Spec role | Implemented as | Can |
|---|---|---|
| Admin / System Admin | superuser | everything, incl. users, roles, subject assignments — but never approves own work |
| Teacher | Teacher / Subject Expert | upload materials, write questions and blueprints for assigned subjects |
| Reviewer / Head Teacher | Head of Department | review blueprints, approve questions, for assigned subjects; no final papers, no subject-assignment edits |
| — (decision 1) | Exam Controller | generate, see, edit, lock and export confidential final papers; manage the academic calendar; sees all subjects |
| Read-Only User | Read-Only | view materials, question bank, calendar for assigned subjects |
| HR Admin | HR Manager | all HR records, reports; view (not change) user accounts |
| Department Head | `Department.head` (not a role) | see the team's records, approve the team's leave (never own) |
| Employee | Employee | own profile, attendance, leave requests |

Exact defaults: `learning/roles.py` `DEFAULT_ROLES`. Academic users see only subjects/grades in their
`SubjectAssignment`s.

## HR scope (basic, not a full HRMS)

Employee profiles (contact, job title, department, joining date), departments, designations and reporting lines;
daily attendance with late arrivals, absences and working hours; leave requests, review and balances; employee
directory by permission; HR dashboard and monthly reports (CSV). Out of scope for now: payroll, recruitment,
performance appraisal, biometric integration.

## Decisions taken

1. **Confidential workflow** (recommended option chosen): Teacher submits blueprint → HOD approves → Exam Controller
   generates and manages the confidential paper. Teachers do not automatically get final-paper export. The
   teacher-led and hybrid (teacher practice papers) workflows are not built; hybrid is still an open question.
2. **RAG deferred** to phase 2/3: improve document processing first (text extraction, chosen chapters/materials,
   limited context), measure token usage, and add chunking/embeddings/vector search only when size, cost or quality
   justify it. Suggested trigger in the plan: typical final-paper requests above ~150k input tokens or "too large to
   send" errors.
3. Also requested: academic-calendar-based and approval-based notifications.

## Status

Built (see the plan's "Already built" table): knowledge base with DOCX/TXT/MD and text extraction; material type,
chapter, topic; AI generation of bank questions from chosen materials with duplicate flagging; blueprints with
types, difficulty, marks, sample paper and guidance; the confidential workflow with no self-approval; background
final-paper generation with count/sections/marks forced from the blueprint; AI review of syllabus fit and answers;
edit, regenerate whole paper, lock; preview/print and PDF/DOCX export with answer key (locked only); audit log;
approval notifications and deadline reminders; subject/grade scoping; profile page; HR employees, departments with
heads and line managers, punch in/out, holidays, leave with balances, working hours, monthly report with CSV.

Open gaps (plan phases):
1. Lock doesn't verify the paper still matches its blueprint (totals, count, distribution) — Phase 1, in progress.
2. Check findings go stale after hand edits — Phase 1.
3. No single-question regenerate — Phase 2.
4. AI token usage isn't measured (input to the RAG decision) — Phase 3, adds model `AIUsage`.
5. No calendar-based warning to exam controllers before exams — Phase 4.
6. No reuse/copy of a blueprint for a new exam — Phase 5.

Open questions for the user (from the plan): re-run AI review after single-question regeneration?; strict lock
validation OK?; show AI cost in money?; hybrid practice papers?
