# Supabase Engineering Guide for Fanoos

*Operational reference for Fanoos development and deployment*

## 1. Purpose

This guide explains how Fanoos can use Supabase safely during the MVP
stage and prepare for future growth. It focuses on daily development,
Python, VS Code, local development, Git, migrations, security, backups,
troubleshooting, and scaling.

This is an engineering guide, not a full SQL course. Keep detailed table
design and table-creation examples in the separate file
`Fanoos_Database_and_SQL_Examples.md`.

Official documentation changes over time. Check the linked pages before
changing production settings.

## 2. What Supabase provides

Supabase is a hosted backend platform built around PostgreSQL.

-   **PostgreSQL** stores data and runs SQL queries.
-   **Supabase APIs and client libraries** let an application read and
    write data.
-   **Auth** manages user identity.
-   **Storage** stores files; the database stores their metadata.
-   **Dashboard** helps developers inspect and manage the project.
-   **CLI and local development tools** help keep database changes in
    files and test them locally.

For Fanoos, PostgreSQL can store jobs, companies, skills, processing
status, and other structured data. Python workers can collect and
process job posts, then save results to the database.

## 3. Main ways to use Supabase

### A. Hosted project (Cloud)

The database runs on Supabase's servers. Developers connect through the
Dashboard, an API, or a database connection.

**Good for:** a shared MVP database, demos, and a deployed application.

**Risks:** access depends on internet connectivity and the hosted
service. A local copy of the code does not make the cloud database
available during an outage.

### B. Local Supabase

The Supabase CLI can run a local development stack, usually through
Docker. It includes a local PostgreSQL database and local versions of
supported Supabase services.

**Good for:** trying schema changes, testing, and continuing some
development when the internet is unavailable.

**Limits:** local data is separate from cloud data. Local Auth, Storage,
and other services do not automatically share state with the hosted
project. Downloading images, installing tools, and first-time setup
require connectivity. External APIs and LLM services may still be
unavailable offline.

Official guide: https://supabase.com/docs/guides/local-development

### C. Python connected to Supabase

Python can connect in two common ways:

1.  **Supabase Python client (`supabase-py`)** --- convenient for API
    operations such as select, insert, update, and delete.
2.  **PostgreSQL driver (for example, `psycopg`)** --- connects to
    PostgreSQL directly and is useful for SQL-heavy tasks, transactions,
    and database tools. Connection type and network support must be
    checked.

These are not separate hosting options: Python can connect to a hosted
project or to a local development project.

For most Fanoos application code, start with the Supabase Python client
when its API is enough. Use direct PostgreSQL access when a task needs
SQL features or transaction control that fits better with a database
driver.

Python reference:
https://supabase.com/docs/reference/python/introduction

## 4. Connect from Python

### Install

``` bash
python -m pip install supabase python-dotenv
```

### Store credentials in `.env`

``` env
SUPABASE_URL=https://YOUR_PROJECT_REF.supabase.co
SUPABASE_KEY=YOUR_SERVER_SIDE_KEY
```

Use the current key recommended for your project. Keep secrets out of
source control. Never put a secret/service-role key in browser code or
commit it to Git. For server-side jobs, grant only the access needed and
review the project's current key model.

Add these patterns to `.gitignore`:

``` gitignore
.env
.env.*
!.env.example
```

You may commit a safe `.env.example` containing placeholder values,
never real credentials.

### Minimal Python example

This example assumes a table called `jobs` exists and has `id`, `title`,
and `company` columns. See the separate SQL guide for a sample table.

``` python
import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

url = os.environ["SUPABASE_URL"]
key = os.environ["SUPABASE_KEY"]

supabase = create_client(url, key)

# Read a few rows
response = (
    supabase.table("jobs")
    .select("id, title, company")
    .limit(10)
    .execute()
)

for job in response.data:
    print(job["title"], job["company"])

# Insert one row
response = (
    supabase.table("jobs")
    .insert({
        "title": "Python Developer",
        "company": "Example Company"
    })
    .execute()
)
```

### Update and delete carefully

Always use a filter when updating or deleting specific records.

``` python
# Update one known row
supabase.table("jobs").update(
    {"company": "Updated Company"}
).eq("id", 123).execute()

# Delete one known row
supabase.table("jobs").delete().eq("id", 123).execute()
```

Do not run update/delete code against production until you have checked
the filter and the expected number of affected rows. For important bulk
changes, test on local or staging data first.

### Important query notes

-   Select only the columns you need; avoid `select("*")` in routine
    production queries.
-   Supabase projects commonly have a default maximum of 1,000 rows
    returned by the API. Use pagination for larger result sets and check
    the current API settings.
-   Add suitable filters and database indexes as the data grows.
-   For bulk imports, test batch size and error handling. Avoid sending
    one network request per row when a batch operation is appropriate.
-   Treat external input as untrusted. Validate it before storing it.
-   Add retry logic only for transient failures, and make writes safe to
    retry where possible.

Python query references: - Select:
https://supabase.com/docs/reference/python/select - Insert:
https://supabase.com/docs/reference/python/insert - Update:
https://supabase.com/docs/reference/python/update - Delete:
https://supabase.com/docs/reference/python/delete

## 5. VS Code workflow

VS Code is the editor; it does not itself manage the hosted database.
Your code runs in a Python environment and connects using the
credentials and URL in environment variables.

Recommended setup:

1.  Open the Fanoos repository in VS Code.
2.  Create or select the project's virtual environment.
3.  Install dependencies from the project's dependency file.
4.  Put local credentials in `.env`, which is ignored by Git.
5.  Run a small read-only query first.
6.  Test writes against local or staging data before production.
7.  Keep database schema changes in migration files rather than relying
    only on manual Dashboard changes.

The Dashboard remains useful for inspecting rows and monitoring
settings. Use it carefully: a manual schema change may not be recorded
in Git unless you capture it in a migration.

## 6. Local development with Docker and the Supabase CLI

**Docker** runs applications in isolated containers. The Supabase local
stack uses containers for the local services. **Supabase CLI** is a
terminal tool for creating the local setup, managing migrations, linking
a local project to a hosted project, and running database commands.

Install the CLI and Docker-compatible runtime using the official
instructions:
https://supabase.com/docs/guides/local-development/cli/getting-started

Typical commands in a repository:

``` bash
supabase init
supabase start
supabase status
supabase stop
```

The exact install command depends on the operating system and package
manager. Follow the current official guide rather than copying an old
install command.

Local API credentials and URLs are different from cloud credentials. Use
separate environment files or environment-variable sets for local and
cloud use. Never accidentally point a destructive test at production.

### Moving an existing cloud project to local development

Fanoos already has a hosted project, so do not blindly start by creating
new migrations or pushing schema changes. First agree on a migration
plan and create a backup.

A careful high-level process is:

1.  Back up the hosted database and confirm that the team can access the
    backup.
2.  Initialize Supabase CLI in the repository.
3.  Link the local folder to the correct hosted project.
4.  Pull or capture the existing schema using the current CLI workflow.
5.  Review the generated files and compare them with the real hosted
    schema.
6.  Start local Supabase and test the schema.
7.  From then on, make new schema changes as migrations and review them
    in Git.
8.  Apply migrations to the hosted database only after review and
    testing.

Official workflow:
https://supabase.com/docs/guides/local-development/cli-workflows

**Safety note:** the exact commands and migration-history steps depend
on the project's current state. Inspect the CLI output and current
documentation. Do not run `db reset`, `db push`, or other potentially
destructive commands against production without understanding their
effect.

## 7. Git and migrations

Git tracks code and files. A migration is a versioned SQL file
describing a database schema change. Together, they let team members
review and repeat changes instead of relying on undocumented Dashboard
edits.

Example migration filename:

``` text
supabase/migrations/20261015090000_add_job_source.sql
```

Example content:

``` sql
ALTER TABLE public.jobs
ADD COLUMN IF NOT EXISTS source TEXT;
```

A good team workflow:

1.  Create a Git branch for the task.
2.  Create a migration for a schema change.
3.  Test it locally.
4.  Commit the migration with the related application code.
5.  Open a Pull Request and ask another team member to review it.
6.  Apply the change to staging or a test project.
7.  Apply it to production through the agreed deployment process.
8.  Check logs and the application after deployment.

Team rules:

-   Do not let multiple developers make untracked schema changes in the
    shared production database.
-   Do not edit an already-applied migration as if it had never run;
    create a new migration for the correction.
-   Avoid destructive changes in one step. For example, add a new
    column, update application code, migrate data, and remove an old
    column later.
-   Keep local seed/test data separate from real production data.
-   Decide who can deploy database changes to production.

Official guide:
https://supabase.com/docs/guides/deployment/database-migrations

## 8. Security basics

-   Keep secrets in environment variables or a secrets manager, not in
    Git.
-   Never expose a service-role or other privileged secret in a browser
    or public client.
-   Use separate credentials for development and production.
-   Enable and test Row Level Security (RLS) for tables exposed through
    the Data API when appropriate.
-   Give database roles only the permissions they need.
-   Validate input in the application; do not assume that a UI check
    protects the database.
-   Do not log passwords, access tokens, or full sensitive records.
-   Restrict who can access the Dashboard and who can deploy changes.
-   Review access policies when adding a new table or endpoint.

Official guide: https://supabase.com/docs/guides/database/secure-data

## 8.1 Teamwork on the Free Plan: Roles, Access, and Activity Tracking

**Plan check:** The details in this section were checked against Supabase's official Access Control documentation and pricing page on 15 October 2026. Plan features can change, so re-check the links before making a team or security decision.

### What the Free Plan supports

- **Unlimited dashboard team members.** There is no fixed team-member count limit shown for the Free Plan.
- **Three roles:** Owner, Administrator, and Developer.
- **Organization-level access only.** On Free, a role applies to the organization and its projects. You cannot give someone access to only the Fanoos project through project-scoped roles.
- **No dashboard Read-Only role.** Read-only and project-scoped roles are Team/Enterprise features. Do not assume a Developer is read-only.
- **No Platform Audit Logs.** Free does not provide the organization-wide audit feed for actions such as inviting members or changing project settings. The pricing page also lists Platform Audit Logs as unavailable on Pro; they are listed for Team and Enterprise.

Official plan details: https://supabase.com/pricing
Official access rules: https://supabase.com/docs/guides/platform/access-control

### Roles available on Free

| Role | What it means | Recommended use |
|---|---|---|
| **Owner** | Full control over the organization and its project resources. | Keep this for the main person responsible for the project. Avoid giving it to every developer. |
| **Administrator** | Broad control over projects and project settings. It cannot update organization settings, transfer projects outside the organization, or add new Owners. | Give it only to a trusted maintainer who must manage project settings and team operations. |
| **Developer** | Can view organization resources and work with project content, but cannot change project settings. This is **not** a read-only role; project content includes database work. | Usually the best fit for trusted teammates who need to develop against the project, but only if they are allowed to change project data or content. |

Because Free roles are organization-scoped, a member's permissions apply to the projects in that organization, including future projects. If Fanoos later creates another project inside the same organization, do not assume existing members are excluded from it.

### How to invite a teammate

The Dashboard wording may change slightly, but the general process is:

1. Sign in to the Supabase Dashboard and select the organization that contains Fanoos.
2. Open the organization's team/member settings.
3. Choose **Invite member**, enter the teammate's email, and select **Owner**, **Administrator**, or **Developer**.
4. Send the invitation. The invited person must accept it; Supabase documents that invitations are valid for 24 hours.
5. Confirm that the member appears in the organization's member list and that the role is correct.

Give each teammate their **own Supabase account**. Never share one owner's login. Before inviting someone, decide whether they truly need Dashboard access: on Free you cannot assign them a built-in dashboard Read-Only role or limit their platform role to only one project.

### A safe setup for the Fanoos MVP

- Keep **one primary Owner** and, if needed, a small number of backup Owners.
- Give **Administrator** only to people who need to manage project settings and team membership.
- Give **Developer** to trusted contributors who need to work with the database or project content. Remember that this role can make changes; it is not suitable for someone who must only inspect data.
- Do not invite a person to the Dashboard just because they need to view a report or a few rows. For that case, provide a restricted internal view, a controlled export, or a carefully configured read-only database/application access path instead.
- Use separate development/test data where possible. Review every production schema change through a migration and Git Pull Request before applying it.
- Enable account-level multi-factor authentication where available. The Free Plan does not include organization-level MFA enforcement, so each member should secure their own account.

### Can we see who was last online and what each member did?

**Not fully on the Free Plan.** These are different kinds of activity:

| Activity | What Free can tell you | Important limit |
|---|---|---|
| Team member's last Dashboard visit | There is no organization-wide Platform Audit Log feed on Free for tracking every teammate's Dashboard activity. | Do not assume the Dashboard will show a reliable “last seen” time for each team member. |
| Dashboard actions (for example, changing settings or inviting a member) | The platform-level audit feed is not available on Free. | Pro also does not include Platform Audit Logs according to the current pricing page; the feature is listed for Team and Enterprise. |
| Fanoos application's end users | Supabase Auth can show application users and their `last_sign_in_at` value. Free Auth Audit Logs have a short retention window (listed as 1 hour). | These are **Fanoos app users**, not the developers invited to the Supabase Dashboard. Check current quotas and retention on the pricing page. |
| API/database requests and errors | The Free Plan lists API and Database log retention as 1 day. | These logs help debug requests; they are not a reliable, person-by-person history of all Dashboard actions. |
| Current database connections | Connection reports and PostgreSQL activity views can show connections/sessions. | One Python worker or pooled application can open connections without a developer being online. A connection count is not a team-member count. |
| Who changed a Fanoos row | You can build this into the application using a verified user ID and an audit table or fields such as `created_by` and `updated_by`. | A shared server-side API key does not tell the database which human initiated each operation unless the application records that identity. |

References:
- Auth users: https://supabase.com/docs/guides/auth/users
- Auth Audit Logs: https://supabase.com/docs/guides/auth/audit-logs
- Platform Audit Logs: https://supabase.com/docs/guides/security/platform-audit-logs
- Database connections: https://supabase.com/docs/guides/database/connection-management
- Current plan limits: https://supabase.com/pricing

### How to keep an activity record without paid Platform Audit Logs

For the MVP, use a simple process instead of trying to reconstruct every action from connection logs:

1. **Schema changes:** record them as SQL migration files in Git. Review them through Pull Requests and keep commits linked to the change.
2. **Production access:** agree that only one or two maintainers can apply reviewed migrations to the hosted production database.
3. **Manual Dashboard changes:** avoid direct production schema edits when possible. If an emergency edit is required, record who made it, when, why, and create a matching migration afterward.
4. **Application data changes:** if Fanoos needs a record of who changed a job/company/skill record, add application-level audit fields or an audit-events table and write the verified application user ID with the change.
5. **Debugging:** use the available API/database logs promptly because Free retention is short. Do not treat those logs as a complete security audit trail.

**Key distinction:** Supabase Dashboard roles control who can manage the platform. PostgreSQL grants and Row Level Security (RLS) control access to database objects and application data. These are related but different layers. RLS should not be treated as a replacement for careful Dashboard membership and credentials management.

## 9. Backups and recovery

A backup is useful only if the team can restore it. Do not rely on a
single copy of important data.

For the MVP:

-   Check which backup features the current plan includes.
-   Export important data regularly and store a copy outside the hosted
    project.
-   Keep database backups separate from source code and secrets.
-   Remember that a database backup may not contain the actual files
    stored through Supabase Storage.
-   Document who can restore the database.
-   Test a restore into a separate project or local environment before
    you need it.

Free-plan projects should not assume that the same automatic backup
options available on paid plans apply. Check the current official backup
page: https://supabase.com/docs/guides/platform/backups

## 10. Common problems

  -----------------------------------------------------------------------
  Problem                             First checks
  ----------------------------------- -----------------------------------
  Connection timeout                  Internet access, project URL,
                                      host/port, firewall, and whether
                                      the cloud project is available

  Authentication or permission error  Correct key, role permissions, RLS
                                      policies, and whether the key
                                      belongs to the intended environment

  Table or column not found           Correct schema, spelling, migration
                                      status, and whether the code points
                                      to the expected project

  Query returns fewer rows than       API row limit, filters, pagination,
  expected                            and RLS

  Insert fails                        Required fields, data types,
                                      constraints, duplicate keys, and
                                      error details

  Slow queries                        Query plan, indexes, selected
                                      columns, result size, and database
                                      resource usage

  Too many connections                Use an appropriate
                                      pooler/connection method; avoid
                                      opening a new connection for every
                                      operation

  Works locally but fails in cloud    Environment variables, schema
                                      differences, permissions,
                                      extensions, and migration status

  Changes appear in Dashboard but not Capture the change as a migration
  in Git                              and review it before continuing
  -----------------------------------------------------------------------

Do not hide errors with broad `except Exception: pass`. Log enough
information to debug the issue without exposing secrets.

## 11. Internet reliability and working from Iran

A hosted Supabase project requires a working network path from the
developer or application to Supabase. If that path is slow, filtered, or
unavailable, cloud requests may fail. Do not assume that the service
will always be reachable from every network in Iran; test the actual
connection from the team's networks and deployment environment.

A local Supabase setup can help developers continue schema work and
local tests when the internet is unavailable, once tools and container
images have already been installed. However:

-   Local data does not automatically sync with the cloud.
-   Local development does not keep a deployed production application
    online if that application depends on the hosted project.
-   External APIs, LLM providers, package downloads, and source websites
    may also be unavailable.
-   Keep independent backups and a documented restore/migration process.
-   Check provider terms, data location, and operational requirements
    before storing sensitive data.

Region selection can affect latency and data location. It does not
guarantee compliance or network reachability. Region guide:
https://supabase.com/docs/guides/platform/regions

## 12. Scaling: MVP to a larger product

There is no single row count that tells you when Supabase stops being
suitable. Performance depends on query patterns, indexes, write rate,
connections, data size, compute, and the application's architecture.

### MVP --- current stage

Supabase is often a sensible choice for a small team because it reduces
setup and operations work.

Recommended now: - Keep the schema simple and documented. - Use
migrations and Git. - Keep heavy scraping and LLM work in Python
workers, not in user-facing database requests. - Use batch writes where
suitable. - Add indexes for real query patterns, not every column. -
Monitor query speed, errors, database size, and cost. - Maintain
independent backups.

### Growing usage

When requests or data grow: - Measure slow queries before increasing
resources. - Review indexes and pagination. - Use connection pooling
where appropriate. - Separate long-running jobs from API request
handling. - Consider a queue for retryable background tasks. - Load-test
with realistic data and concurrent users. - Consider read replicas if
read traffic is the bottleneck and the plan/workload supports them.
Replicas can lag behind the primary database.

### Larger scale or strict uptime needs

Before adding complex infrastructure, identify the actual bottleneck.
Options may include larger compute, read replicas, a separate analytics
store, dedicated search infrastructure, or moving selected workloads to
separate services. These add cost and operational complexity; they are
not required just because the project grows.

Compute guide:
https://supabase.com/docs/guides/platform/compute-and-disk Read
replicas: https://supabase.com/docs/guides/platform/read-replicas

## 13. Fanoos MVP checklist

Before calling the setup team-ready, confirm:

-   [ ] The team knows which Supabase project is development, staging,
    and production.
-   [ ] Secrets are not committed to Git.
-   [ ] Current tables and schema are captured and reviewed.
-   [ ] New schema changes use migrations.
-   [ ] A local or staging test process exists.
-   [ ] Production writes and destructive queries are reviewed.
-   [ ] RLS and database permissions are understood and tested.
-   [ ] Backups exist outside the project and restoration has been
    tested.
-   [ ] The team has checked connectivity from its actual networks.
-   [ ] Someone monitors database errors, query performance, and usage
    costs.

## 14. Official references

-   Supabase Docs: https://supabase.com/docs
-   Python client:
    https://supabase.com/docs/reference/python/introduction
-   Local development and CLI:
    https://supabase.com/docs/guides/local-development
-   Local workflow:
    https://supabase.com/docs/guides/local-development/cli-workflows
-   Database migrations:
    https://supabase.com/docs/guides/deployment/database-migrations
-   Security: https://supabase.com/docs/guides/database/secure-data
-   Backups: https://supabase.com/docs/guides/platform/backups
-   Compute and disk:
    https://supabase.com/docs/guides/platform/compute-and-disk
-   Regions: https://supabase.com/docs/guides/platform/regions
-   Python select: https://supabase.com/docs/reference/python/select
-   Python insert: https://supabase.com/docs/reference/python/insert
-   Python update: https://supabase.com/docs/reference/python/update

------------------------------------------------------------------------

**Project note:** This guide is a starting standard for the Fanoos MVP.
Before changing the existing hosted database, review its current schema,
backup state, permissions, and migration history with the team.
