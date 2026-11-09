# Role & Permission Specification: STS Pulse

## 1. Overview & Architecture

STS Pulse utilizes standard Django `auth.User` and `auth.Group` models (aliased as "Roles" in the UI) to deliver fine-grained Role-Based Access Control (RBAC). 

Key architectural principles:
1. **Dynamic Group Synchronization**: Default roles and their permissions are established upon database migration (`post_migrate` hook in `learning/roles.py`). Custom administrator adjustments persist unless explicitly reset.
2. **Confidentiality Lock**: The sensitive permission `questionbank.manage_final_papers` is strictly stripped from all default roles except **Exam Controller** and Superusers via `keep_final_papers_confidential`.
3. **No Self-Approval Enforcement**: Handled via `learning.approvals.is_own` across Academic and HR review queues.
4. **Subject Scoping**: Non-superusers are scoped exclusively to subjects and grades mapped to them in `SubjectAssignment`.

---

## 2. Predefined System Roles (`DEFAULT_ROLES`)

### 2.1 Role Matrix

| Role Name | Primary Responsibilities | Core Django Permissions | Scope Boundaries |
|---|---|---|---|
| **Super Admin** (Superuser) | Full system administration, user provisioning, role permission editing, global audit log inspection. | `*.*` (All permissions across all models) | Unrestricted across all subjects, grades, and departments. Cannot approve own work. |
| **Teacher / Subject Expert** | Material uploads, practice question authoring, blueprint creation and drafting. | `questionbank.add_knowledgesource`, `questionbank.view_knowledgesource`, `questionbank.add_question`, `questionbank.change_question` (draft only), `questionbank.add_questionblueprint`, `questionbank.change_questionblueprint` (draft/returned only) | Restricted to assigned Subjects & Grades. Cannot access final papers. |
| **Head of Department (HOD)** | Academic quality review, question validation, blueprint approval, team member leave review. | `questionbank.view_knowledgesource`, `questionbank.view_question`, `questionbank.approve_question`, `questionbank.review_blueprint`, `hr.view_leave_request`, `hr.approve_leave_request` | Restricted to assigned Academic Subjects & Grades, and assigned HR Department employees. |
| **Exam Controller** | Master generation, prompt tuning, verification, locking, and export of confidential final examination papers. | `questionbank.manage_final_papers`, `questionbank.view_academicevent`, `questionbank.add_academicevent`, `questionbank.change_academicevent` | Unrestricted subject scope for final paper generation and calendar operations. |
| **Read-Only User** | Observational academic review, curriculum research, calendar review. | `questionbank.view_knowledgesource`, `questionbank.view_question`, `questionbank.view_questionblueprint`, `questionbank.view_academicevent` | Restricted to assigned Subjects & Grades. Cannot edit or approve. |
| **HR Manager** | Employee onboarding, records maintenance, designation/department configuration, attendance reporting. | `hr.*` (Full management of HR models), `auth.view_user` | Global HR workspace access. View-only access to user accounts (no privilege escalation). |
| **Employee** | Self-service attendance punching, holiday tracking, personal leave application. | `hr.view_attendance`, `hr.add_attendance`, `hr.view_leaverequest`, `hr.add_leaverequest` | Limited strictly to own employee record (`request.user.employee`). |

---

## 3. Academic Subject & Grade Scoping (`questionbank/scope.py`)

Academic querysets must not expose foreign subject materials or exam drafts. The system implements queryset scoping at the database layer:

```python
# Queryset filtering pattern
qs = in_scope(qs, request.user, prefix="subject__")
```

### Scoping Rules:
1. **Superusers and Exam Controllers**: Bypass subject filtering; receive all subjects and grades.
2. **Standard Academic Users**: Mapped to `SubjectAssignment` records:
   - `SubjectAssignment(user=user, subject=physics, grade=grade_10)`: Access limited to Physics Grade 10.
   - `SubjectAssignment(user=user, subject=math, grade=None)`: Access granted to Mathematics across all grades.
   - **No Assignment**: Querysets return empty sets (`qs.none()`).

---

## 4. Separation of Duties: No Self-Approval Rule

To comply with accreditation standards and institutional governance:
- **Questions**: A teacher or HOD cannot approve their own drafted question (`Question.created_by == request.user`).
- **Blueprints**: An author cannot approve their own blueprint (`QuestionBlueprint.assigned_to == request.user`).
- **Leave Requests**: An employee—including department heads or superusers—cannot approve their own leave request (`LeaveRequest.employee.user == request.user`).

Attempts to approve one's own record raise a validation error displaying `SELF_APPROVAL_MESSAGE` and record a warning event in the audit log.

---

## 5. Privilege Escalation Guards (`learning/admin.py`)

1. **Role Grant Restrictions**: Non-superusers with user-management privileges are constrained to assigning only those roles/permissions that they personally hold.
2. **Confidential Paper Protection**: Even if an admin user attempts to manually grant `manage_final_papers` to a teacher group in the UI, `keep_final_papers_confidential` strips the permission during automated audits and migrations.

