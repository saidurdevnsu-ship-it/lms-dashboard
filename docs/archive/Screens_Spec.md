# Screens & UI Specification: STS Pulse

## 1. Design System & Frontend Architecture

STS Pulse renders its entire UI within an enhanced Django Admin framework, customized via semantic CSS variables and responsive component partials located in `templates/admin/`.

### 1.1 Color Palette & Typography
- **Primary Brand**: STS Deep Blue (`#1e3a8a`, hover: `#172554`)
- **Accent**: Slate Indigo (`#4338ca`)
- **Neutral Backgrounds**: Canvas `#f8fafc`, Card Surface `#ffffff`, Border `#e2e8f0`
- **Text Hierarchy**: Headings `#0f172a`, Body `#334155`, Muted `#64748b`
- **Status Indicators**:
  - Success / Approved: `#16a34a` (Green)
  - Warning / In Review: `#d97706` (Amber)
  - Danger / Rejected: `#dc2626` (Red)
  - Confidential / Locked: `#7c3aed` (Purple)

---

## 2. Screen Inventory & Component Hierarchy

### 2.1 Authentication & Workspace Chooser

| Screen Name | Route / Template | Primary Components | User Actions |
|---|---|---|---|
| **Sign In** | `/accounts/login/`<br>`templates/registration/login.html` | Logo header, Email/Username input, Password input, "Remember Me" toggle, Sign-in button | Authenticate, toggle session persistence |
| **Workspace Chooser** | `/workspace/`<br>`templates/registration/workspace.html` | Academic card, HR card, role permission badges | Select target domain workspace |
| **User Profile** | `/profile/`<br>`templates/registration/profile.html` | Profile overview, linked Employee details, assigned subjects list, password change | Update personal contact info |

---

### 2.2 Academic Workspace Screens (`questionbank`)

#### 2.2.1 Academic Home Dashboard
- **Route**: `/admin/` (Active workspace: `academic`)
- **Template Partials**:
  - `_hero.html`: Academic term status, active exam series banner.
  - `_attention.html`: Urgent tasks (blueprints requiring HOD review, pending questions).
  - `_quick_actions.html`: Shortcuts to "Upload Materials", "Generate Questions", "Create Blueprint".
  - `_coming_up.html`: Upcoming academic calendar milestones and question submission deadlines.

#### 2.2.2 Knowledge Source Repository
- **Route**: `/admin/questionbank/knowledgesource/`
- **Views**: ChangeList with subject/grade/type filters; ChangeForm with file dropzone and text extraction status badge.

#### 2.2.3 AI Question Generator Form
- **Route**: `/admin/questionbank/question/generate/`
- **Template**: `templates/admin/questionbank/question/generate.html`
- **Components**:
  - Multi-select dropdown for active Knowledge Sources.
  - Question Type selector (MCQ, Short Answer, Long Essay, True/False, Fill in Blank).
  - Difficulty slider / radio buttons (Easy, Medium, Hard).
  - Number of questions counter.
  - Real-time generation progress bar.

#### 2.2.4 Question Review & Bank ChangeList
- **Route**: `/admin/questionbank/question/`
- **Components**: Status pill filters (`Draft`, `Approved`, `Rejected`), duplicate question warning callouts, inline HOD "Approve" / "Reject" buttons (disabled on own questions).

#### 2.2.5 Exam Blueprint Builder
- **Route**: `/admin/questionbank/questionblueprint/add/` and `.../change/`
- **Template**: `templates/admin/questionbank/questionblueprint/change_form.html`
- **Components**:
  - Header: Subject, Grade, Target Exam, Due Date, Total Marks counter.
  - Tabular inline for `BlueprintItem` (Section name, question type, difficulty, question count, marks each).
  - Blueprint workflow toolbar: `Submit for Review`, `Approve Blueprint`, `Return to Author`.

#### 2.2.6 Confidential Final Exam Paper Editor (Exam Controller Only)
- **Route**: `/admin/questionbank/finalpaper/<id>/change/`
- **Template**: `templates/admin/questionbank/finalpaper/change_form.html`
- **Components**:
  - Security header indicating confidential handling.
  - "Generate with AI" button with async background worker status poller.
  - Automated AI check findings panel (syllabus alignment and answer correctness).
  - Inline question reorder, inline question editing, single-question regeneration.
  - "Lock Paper" button (disables editing, locks blueprint conformance).
  - Export action bar: "Export PDF", "Export Word", "Download Answer Key".

---

### 2.3 HR Workspace Screens (`hr`)

#### 2.3.1 HR Home Dashboard & Punch Widget
- **Route**: `/admin/` (Active workspace: `hr`)
- **Template**: `templates/admin/hr/_punch.html`
- **Components**:
  - Real-time Asia/Dhaka digital clock.
  - Punch In / Punch Out toggle button with working hours timer.
  - Today's attendance summary card.
  - Department leave approval queue.

#### 2.3.2 Attendance ChangeList & Monthly Report
- **Route**: `/admin/hr/attendance/` and `/admin/hr/attendance/report/`
- **Template**: `templates/admin/hr/attendance/report.html`
- **Components**:
  - Monthly calendar matrix displaying employee check-in/out times.
  - Colored status pills (`Present`, `Late`, `Half Day`, `Absent`, `On Leave`).
  - "Export Monthly CSV" action button.

#### 2.3.3 Leave Application & Approval Form
- **Route**: `/admin/hr/leaverequest/`
- **Components**:
  - Leave balance progress rings (e.g., Casual Leave 8/12 days remaining).
  - Working days auto-calculator (automatically excludes weekends and `Holiday` records).
  - Line manager / HOD review box with approval action buttons.

---

### 2.4 Standalone UI Mockup
For interactive review and presentation to stakeholders, a standalone HTML mockup is provided at [**ui-mockup.html**](file:///c:/Users/User/Desktop/projects/lms/lms_dashboard/docs/ui-mockup.html).

