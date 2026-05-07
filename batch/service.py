from typing import List, Optional
from uuid import UUID
import secrets
import string

from database import get_supabase
from exceptions import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from schemas import UserOut, UserRole
from .schemas import BatchCreate, BatchUpdate, BatchOut, BatchDetailOut, EnrollmentOut


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _generate_batch_code(length: int = 8) -> str:
    """Generates a short uppercase alphanumeric code, e.g. 'A3XK9P2Q'."""
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ---------------------------------------------------------------------------
# Batch CRUD
# ---------------------------------------------------------------------------

async def create_batch(teacher: UserOut, data: BatchCreate) -> BatchOut:
    """
    Teacher creates a new batch.
    Generates a unique batch_code (retries on collision).
    Inserts into `batches` table and returns the created row as BatchOut.
    """
    if teacher.role != UserRole.TEACHER:
        raise ForbiddenError("Only teachers can create batches")

    supabase = get_supabase()

    # Ensure batch_code uniqueness (collision is astronomically unlikely but handled)
    for _ in range(5):
        code = _generate_batch_code()
        existing = supabase.table("batches").select("id").eq("batch_code", code).execute()
        if not existing.data:
            break
    else:
        raise Exception("Failed to generate a unique batch code, please try again")

    row = {
        "teacher_id": str(teacher.id),
        "name": data.name,
        "description": data.description,
        "batch_code": code,
    }
    response = supabase.table("batches").insert(row).execute()
    if not response.data:
        raise Exception("Failed to create batch")

    return BatchOut(**response.data[0])


async def get_teacher_batches(teacher: UserOut) -> List[BatchDetailOut]:
    """
    Returns all batches owned by the teacher, each annotated with enrolled student count.
    Fetches from `batches`, then counts rows in `batch_enrollments` per batch.
    """
    if teacher.role != UserRole.TEACHER:
        raise ForbiddenError("Only teachers can view their batches")

    supabase = get_supabase()
    response = supabase.table("batches").select("*").eq("teacher_id", str(teacher.id)).order("created_at", desc=True).execute()
    batches = response.data or []

    result = []
    for b in batches:
        count_resp = supabase.table("batch_enrollments").select("id", count="exact").eq("batch_id", b["id"]).execute()
        student_count = count_resp.count or 0
        result.append(BatchDetailOut(**b, student_count=student_count))

    return result


async def get_batch(batch_id: UUID, requester: UserOut) -> BatchDetailOut:
    """
    Fetches a single batch by ID.
    Teacher must own the batch; students must be enrolled in it.
    Returns BatchDetailOut with student count.
    """
    supabase = get_supabase()
    response = supabase.table("batches").select("*").eq("id", str(batch_id)).single().execute()
    if not response.data:
        raise NotFoundError("Batch not found")

    batch = response.data

    if requester.role == UserRole.TEACHER:
        if batch["teacher_id"] != str(requester.id):
            raise ForbiddenError("You do not own this batch")
    elif requester.role == UserRole.STUDENT:
        enrollment = supabase.table("batch_enrollments").select("id").eq("batch_id", str(batch_id)).eq("user_id", str(requester.id)).execute()
        if not enrollment.data:
            raise ForbiddenError("You are not enrolled in this batch")

    count_resp = supabase.table("batch_enrollments").select("id", count="exact").eq("batch_id", str(batch_id)).execute()
    student_count = count_resp.count or 0

    return BatchDetailOut(**batch, student_count=student_count)


async def update_batch(batch_id: UUID, teacher: UserOut, data: BatchUpdate) -> BatchOut:
    """
    Teacher updates name/description of a batch they own.
    Only provided (non-None) fields are updated.
    Returns the updated BatchOut.
    """
    if teacher.role != UserRole.TEACHER:
        raise ForbiddenError("Only teachers can update batches")

    supabase = get_supabase()

    # Verify ownership
    existing = supabase.table("batches").select("teacher_id").eq("id", str(batch_id)).single().execute()
    if not existing.data:
        raise NotFoundError("Batch not found")
    if existing.data["teacher_id"] != str(teacher.id):
        raise ForbiddenError("You do not own this batch")

    update_payload = {k: v for k, v in data.model_dump().items() if v is not None}
    if not update_payload:
        raise BadRequestError("No fields to update")

    response = supabase.table("batches").update(update_payload).eq("id", str(batch_id)).execute()
    if not response.data:
        raise Exception("Failed to update batch")

    return BatchOut(**response.data[0])


async def delete_batch(batch_id: UUID, teacher: UserOut) -> None:
    """
    Teacher deletes a batch they own.
    Also deletes all batch_enrollments rows for that batch (cascade expected in DB,
    but we explicitly delete here for safety).
    """
    if teacher.role != UserRole.TEACHER:
        raise ForbiddenError("Only teachers can delete batches")

    supabase = get_supabase()

    existing = supabase.table("batches").select("teacher_id").eq("id", str(batch_id)).single().execute()
    if not existing.data:
        raise NotFoundError("Batch not found")
    if existing.data["teacher_id"] != str(teacher.id):
        raise ForbiddenError("You do not own this batch")

    # Remove enrollments first (in case DB cascade is not configured)
    supabase.table("batch_enrollments").delete().eq("batch_id", str(batch_id)).execute()
    supabase.table("batches").delete().eq("id", str(batch_id)).execute()


# ---------------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------------

async def join_batch_by_code(student: UserOut, batch_code: str) -> EnrollmentOut:
    """
    Student self-enrolls using a batch_code.
    Looks up the batch by code, checks the student isn't already enrolled,
    inserts into `batch_enrollments` with enrolled_via='batch_code', returns EnrollmentOut.
    """
    if student.role != UserRole.STUDENT:
        raise ForbiddenError("Only students can join batches")

    supabase = get_supabase()

    # Resolve batch
    batch_resp = supabase.table("batches").select("id").eq("batch_code", batch_code.upper()).execute()
    if not batch_resp.data:
        raise NotFoundError("Invalid batch code")

    batch_id = batch_resp.data[0]["id"]

    # Check duplicate enrollment
    existing = supabase.table("batch_enrollments").select("id").eq("batch_id", batch_id).eq("user_id", str(student.id)).execute()
    if existing.data:
        raise ConflictError("You are already enrolled in this batch")

    row = {
        "batch_id": batch_id,
        "user_id": str(student.id),
        "enrolled_via": "batch_code",
    }
    response = supabase.table("batch_enrollments").insert(row).execute()
    if not response.data:
        raise Exception("Failed to enroll in batch")

    return EnrollmentOut(**response.data[0])


async def enroll_student_manual(batch_id: UUID, student_id: UUID, teacher: UserOut) -> EnrollmentOut:
    """
    Teacher manually enrolls a specific student into a batch they own.
    Inserts into `batch_enrollments` with enrolled_via='manual'.
    Used for support/admin overrides.
    """
    if teacher.role != UserRole.TEACHER:
        raise ForbiddenError("Only teachers can manually enroll students")

    supabase = get_supabase()

    # Verify ownership
    batch_resp = supabase.table("batches").select("teacher_id").eq("id", str(batch_id)).single().execute()
    if not batch_resp.data:
        raise NotFoundError("Batch not found")
    if batch_resp.data["teacher_id"] != str(teacher.id):
        raise ForbiddenError("You do not own this batch")

    # Verify student exists
    user_resp = supabase.table("users").select("id, role").eq("id", str(student_id)).single().execute()
    if not user_resp.data:
        raise NotFoundError("Student not found")
    if user_resp.data["role"] != UserRole.STUDENT.value:
        raise BadRequestError("Target user is not a student")

    # Check duplicate
    existing = supabase.table("batch_enrollments").select("id").eq("batch_id", str(batch_id)).eq("user_id", str(student_id)).execute()
    if existing.data:
        raise ConflictError("Student is already enrolled in this batch")

    row = {
        "batch_id": str(batch_id),
        "user_id": str(student_id),
        "enrolled_via": "manual",
    }
    response = supabase.table("batch_enrollments").insert(row).execute()
    if not response.data:
        raise Exception("Failed to enroll student")

    return EnrollmentOut(**response.data[0])


async def enroll_student_payment(batch_id: UUID, student_id: UUID) -> EnrollmentOut:
    """
    Internal function called by payment webhook (Phase — Payment Gateway).
    Inserts into `batch_enrollments` with enrolled_via='payment'.
    No auth check — called only from trusted internal payment callback.
    """
    supabase = get_supabase()

    existing = supabase.table("batch_enrollments").select("id").eq("batch_id", str(batch_id)).eq("user_id", str(student_id)).execute()
    if existing.data:
        # Idempotent — payment webhook may fire more than once
        return EnrollmentOut(**existing.data[0])

    row = {
        "batch_id": str(batch_id),
        "user_id": str(student_id),
        "enrolled_via": "payment",
    }
    response = supabase.table("batch_enrollments").insert(row).execute()
    if not response.data:
        raise Exception("Failed to enroll student via payment")

    return EnrollmentOut(**response.data[0])


async def remove_student(batch_id: UUID, student_id: UUID, teacher: UserOut) -> None:
    """
    Teacher removes a student from a batch they own.
    Deletes the enrollment row from `batch_enrollments`.
    """
    if teacher.role != UserRole.TEACHER:
        raise ForbiddenError("Only teachers can remove students")

    supabase = get_supabase()

    batch_resp = supabase.table("batches").select("teacher_id").eq("id", str(batch_id)).single().execute()
    if not batch_resp.data:
        raise NotFoundError("Batch not found")
    if batch_resp.data["teacher_id"] != str(teacher.id):
        raise ForbiddenError("You do not own this batch")

    existing = supabase.table("batch_enrollments").select("id").eq("batch_id", str(batch_id)).eq("user_id", str(student_id)).execute()
    if not existing.data:
        raise NotFoundError("Student is not enrolled in this batch")

    supabase.table("batch_enrollments").delete().eq("batch_id", str(batch_id)).eq("user_id", str(student_id)).execute()


async def get_enrolled_students(batch_id: UUID, teacher: UserOut) -> List[dict]:
    """
    Returns list of enrolled students for a batch the teacher owns.
    Joins `batch_enrollments` with `users` to return user details + enrollment metadata.
    Each item: { id, name, email, role, enrolled_at, enrolled_via }
    """
    if teacher.role != UserRole.TEACHER:
        raise ForbiddenError("Only teachers can view enrolled students")

    supabase = get_supabase()

    batch_resp = supabase.table("batches").select("teacher_id").eq("id", str(batch_id)).single().execute()
    if not batch_resp.data:
        raise NotFoundError("Batch not found")
    if batch_resp.data["teacher_id"] != str(teacher.id):
        raise ForbiddenError("You do not own this batch")

    enrollments = supabase.table("batch_enrollments").select("user_id, enrolled_at, enrolled_via").eq("batch_id", str(batch_id)).execute()
    if not enrollments.data:
        return []

    user_ids = [e["user_id"] for e in enrollments.data]
    users_resp = supabase.table("users").select("id, name, email, role").in_("id", user_ids).execute()

    user_map = {u["id"]: u for u in (users_resp.data or [])}

    result = []
    for e in enrollments.data:
        user = user_map.get(e["user_id"])
        if user:
            result.append({
                **user,
                "enrolled_at": e["enrolled_at"],
                "enrolled_via": e["enrolled_via"],
            })

    return result


async def get_student_batches(student: UserOut) -> List[BatchOut]:
    """
    Returns all batches a student is currently enrolled in.
    Queries `batch_enrollments` for the student's user_id, then fetches each batch row.
    """
    if student.role != UserRole.STUDENT:
        raise ForbiddenError("Only students can view their enrolled batches")

    supabase = get_supabase()

    enrollments = supabase.table("batch_enrollments").select("batch_id").eq("user_id", str(student.id)).execute()
    if not enrollments.data:
        return []

    batch_ids = [e["batch_id"] for e in enrollments.data]
    batches_resp = supabase.table("batches").select("*").in_("id", batch_ids).execute()

    return [BatchOut(**b) for b in (batches_resp.data or [])]