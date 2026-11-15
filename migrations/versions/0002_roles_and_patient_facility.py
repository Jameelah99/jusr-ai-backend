"""Role model update (COORDINATOR, REFERRING_CLINICIAN, RECEIVING_CLINICIAN) and patients.facility_id."""
import sqlalchemy as sa
from alembic import op

revision = "0002_roles_and_patient_facility"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

NEW_ROLES = ("COORDINATOR", "REFERRING_CLINICIAN", "RECEIVING_CLINICIAN")

def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # ADD VALUE cannot be used in the transaction that adds it, so commit first.
        with op.get_context().autocommit_block():
            for role in NEW_ROLES:
                op.execute(f"ALTER TYPE role ADD VALUE IF NOT EXISTS '{role}'")
    # Old name for the coordinator role.
    op.execute("UPDATE users SET role = 'COORDINATOR' WHERE role = 'FACILITY_STAFF'")
    columns = [c["name"] for c in sa.inspect(bind).get_columns("patients")]
    if "facility_id" not in columns:
        # SQLite cannot add a foreign-key constraint with ADD COLUMN; it is enforced on PostgreSQL only.
        fk = [] if bind.dialect.name == "sqlite" else [sa.ForeignKey("facilities.id")]
        op.add_column("patients", sa.Column("facility_id", sa.String(36), *fk, nullable=True))
    op.execute("UPDATE patients SET facility_id = (SELECT c.facility_id FROM cases c WHERE c.patient_id = patients.id LIMIT 1) WHERE facility_id IS NULL")

def downgrade():
    # Enum values cannot be removed from PostgreSQL types; only the column is dropped.
    op.execute("UPDATE users SET role = 'FACILITY_STAFF' WHERE role = 'COORDINATOR'")
    if op.get_bind().dialect.name == "postgresql":
        op.drop_column("patients", "facility_id")
    # SQLite cannot drop a foreign-key column without a table rebuild; the extra nullable column is harmless.
