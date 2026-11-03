"""The predefined roles and their default permissions.

Roles are created with these defaults when missing (after every migrate, and by setup_academics / setup_hr). After
that they belong to the school: super admins edit them under Roles, and nothing overwrites those edits unless someone
asks for a reset (the "Reset to default permissions" action, or a setup command with --reset).
"""

from django.contrib.auth.models import Group, Permission
from django.db.models import Q

FINAL_PAPERS = "questionbank.manage_final_papers"
EXAM_CONTROLLER = "Exam Controller"
ADMIN = "Admin"
# The only roles that may see confidential final papers.
FINAL_PAPER_ROLES = (EXAM_CONTROLLER, ADMIN)
# Roles that write what final papers are made from. Nobody may hold one of these together with a final-paper role,
# or they could write a blueprint and then read the paper made from it.
AUTHORING_ROLES = ("Teacher / Subject Expert", "Head of Department", "Subject Matter Expert")

# Each role: a description and its permissions as "app.codename", or "app.*" for every permission of an app, or
# "*" for every permission there is. "-app.codename" removes one again (applied after the others).
DEFAULT_ROLES = {
    ADMIN: {
        "description": "Full access to everything, including confidential final papers. Creating and editing roles "
                       "stays with super admins.",
        "permissions": ["*"],
    },
    "Subject Matter Expert": {
        "description": "Uploads and manages books and materials for their assigned subjects. Can't write or "
                       "generate questions, and doesn't see blueprints or final papers.",
        "permissions": [
            "questionbank.view_subject", "questionbank.view_grade",
            "questionbank.add_knowledgesource", "questionbank.change_knowledgesource",
            "questionbank.delete_knowledgesource", "questionbank.view_knowledgesource",
        ],
    },
    "Teacher / Subject Expert": {
        "description": "Uploads materials, writes questions and blueprints for their assigned subjects.",
        "permissions": [
            "questionbank.view_subject", "questionbank.view_grade", "questionbank.view_academicevent",
            "questionbank.add_knowledgesource", "questionbank.change_knowledgesource",
            "questionbank.delete_knowledgesource", "questionbank.view_knowledgesource",
            "questionbank.add_question", "questionbank.change_question", "questionbank.view_question",
            "questionbank.add_questionblueprint", "questionbank.change_questionblueprint",
            "questionbank.view_questionblueprint",
            "questionbank.add_blueprintitem", "questionbank.change_blueprintitem",
            "questionbank.delete_blueprintitem", "questionbank.view_blueprintitem",
        ],
    },
    "Head of Department": {
        "description": "Reviews blueprints and approves questions for their assigned subjects.",
        "permissions": ["questionbank.*", f"-{FINAL_PAPERS}",
                        "-questionbank.add_subjectassignment", "-questionbank.change_subjectassignment",
                        "-questionbank.delete_subjectassignment"],
    },
    EXAM_CONTROLLER: {
        "description": "Generates, sees and locks the confidential final papers; manages the academic calendar.",
        "permissions": [
            FINAL_PAPERS, "questionbank.view_subject", "questionbank.view_grade",
            "questionbank.view_knowledgesource", "questionbank.view_academicevent",
            "questionbank.add_academicevent", "questionbank.change_academicevent",
            "questionbank.delete_academicevent", "questionbank.view_questionblueprint",
            "questionbank.view_blueprintitem",
        ],
    },
    "Read-Only": {
        "description": "Views materials, the question bank and the calendar for their assigned subjects.",
        "permissions": ["questionbank.view_subject", "questionbank.view_grade", "questionbank.view_knowledgesource",
                        "questionbank.view_academicevent", "questionbank.view_question"],
    },
    "HR Manager": {
        "description": "Manages employees, attendance, leave and HR reports; can view user accounts.",
        # View-only on accounts: changing users is for super admins (see learning/admin.py).
        "permissions": ["hr.*", "auth.view_user", "auth.view_group"],
    },
    "Employee": {
        "description": "Self-service: own profile, attendance and leave requests.",
        "permissions": ["hr.view_employee", "hr.view_attendance", "hr.view_holiday", "hr.view_leavetype",
                        "hr.add_leaverequest", "hr.view_leaverequest"],
    },
}


def default_permissions(name):
    """The default Permission queryset for a predefined role."""
    include, exclude = Q(pk__in=[]), Q(pk__in=[])
    everything = False
    for entry in DEFAULT_ROLES[name]["permissions"]:
        if entry == "*":
            everything = True
            continue
        target = entry.lstrip("-")
        app, codename = target.split(".")
        q = Q(content_type__app_label=app) if codename == "*" else Q(content_type__app_label=app, codename=codename)
        if entry.startswith("-"):
            exclude |= q
        else:
            include |= q
    return (Permission.objects.all() if everything else Permission.objects.filter(include)).exclude(exclude)


def _is_final_paper_role(name):
    # MySQL compares names ignoring case, Python doesn't; compare the same way everywhere.
    return name.lower() in {n.lower() for n in FINAL_PAPER_ROLES}


def keep_final_papers_confidential(log=None):
    """Only the final-paper roles (Exam Controller, Admin) may hold the final paper permission; take it off any
    other role."""
    app, codename = FINAL_PAPERS.split(".")
    final = Permission.objects.filter(content_type__app_label=app, codename=codename).first()
    if final is None:
        return []
    removed = []
    for group in Group.objects.filter(permissions=final):
        if _is_final_paper_role(group.name):
            continue
        group.permissions.remove(final)
        removed.append(group.name)
        if log:
            log(f"Removed final-paper access from role: {group.name}")
    return removed


# Permissions for writing or approving what final papers are made from.
AUTHORING_PERMISSIONS = ("add_questionblueprint", "change_questionblueprint", "review_blueprint",
                         "add_question", "change_question", "approve_question")


def role_conflict(groups):
    """A message if these roles mix seeing final papers with writing what they're made from, otherwise None.
    Judged by what each role allows, not just its name, so renamed or custom roles are caught too. A single role
    that does both (Admin) is allowed; such people are kept away from their own blueprints' papers by
    questionbank.workflow.can_open_final."""
    groups = list(groups)
    final = [g for g in groups if _is_final_paper_role(g.name)
             or g.permissions.filter(content_type__app_label="questionbank", codename="manage_final_papers").exists()]
    authoring_names = {n.lower() for n in AUTHORING_ROLES}
    authoring = [g for g in groups if g not in final and (
        g.name.lower() in authoring_names
        or g.permissions.filter(content_type__app_label="questionbank", codename__in=AUTHORING_PERMISSIONS).exists())]
    if final and authoring:
        final_names = " or ".join(sorted(g.name for g in final))
        return (f"Someone with {final_names} sees the confidential final papers, so they can't also have "
                f"{', '.join(sorted(g.name for g in authoring))}: they could read the exam made from their own "
                "blueprint or questions. Give these roles to different people.")
    return None


def ensure_roles(names=None, reset=False, log=None):
    """Create missing predefined roles with their defaults; with reset, also restore existing ones.
    Returns {name: "created" | "reset" | "kept"}. An existing role whose name differs only in capitals (MySQL
    treats them as the same) is renamed to the exact predefined name rather than duplicated."""
    result = {}
    for name in names or DEFAULT_ROLES:
        group = Group.objects.filter(name__iexact=name).order_by("pk").first()
        created = group is None
        if created:
            group = Group.objects.create(name=name)
        elif group.name != name:
            if log:
                log(f"Role {group.name}: renamed to {name}")
            group.name = name
            group.save(update_fields=["name"])
        if created or reset:
            group.permissions.set(default_permissions(name))
            result[name] = "created" if created else "reset"
        else:
            result[name] = "kept"
        if log:
            status = {"created": "created with default permissions", "reset": "reset to default permissions",
                      "kept": "already exists, kept as edited (use --reset to restore defaults)"}[result[name]]
            log(f"Role {name}: {status}")
    keep_final_papers_confidential(log)
    return result


def create_missing_roles(sender, **kwargs):
    """post_migrate: make sure every permission exists, then add any predefined role that's missing."""
    from django.apps import apps
    from django.contrib.auth.management import create_permissions

    for app_config in apps.get_app_configs():
        create_permissions(app_config, verbosity=0, using=kwargs.get("using", "default"))
    ensure_roles()
