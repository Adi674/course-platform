from fastapi import APIRouter, Depends, status
from typing import List
from uuid import UUID

from dependencies import get_current_user, require_teacher
from schemas import UserOut
from .schemas import BatchCreate, BatchUpdate, BatchOut, BatchDetailOut, EnrollmentOut, JoinBatchRequest
from . import service

router = APIRouter(prefix="/batches", tags=["batches"])


# ---------------------------------------------------------------------------
# Batch CRUD — Teacher
# ---------------------------------------------------------------------------

@router.post("", response_model=BatchOut, status_code=status.HTTP_201_CREATED)
async def create_batch(
    data: BatchCreate,
    teacher: UserOut = Depends(require_teacher)
):
    """
    Teacher creates a new batch. Auto-generates a unique batch_code.
    """
    return await service.create_batch(teacher, data)


@router.get("", response_model=List[BatchDetailOut])
async def list_my_batches(
    teacher: UserOut = Depends(require_teacher)
):
    """
    Teacher lists all their batches with enrolled student count.
    """
    return await service.get_teacher_batches(teacher)


@router.get("/my", response_model=List[BatchOut])
async def list_student_batches(
    current_user: UserOut = Depends(get_current_user)
):
    """
    Student lists all batches they are enrolled in.
    """
    return await service.get_student_batches(current_user)


@router.get("/{batch_id}", response_model=BatchDetailOut)
async def get_batch(
    batch_id: UUID,
    current_user: UserOut = Depends(get_current_user)
):
    """
    Fetch a single batch. Teachers must own it; students must be enrolled.
    """
    return await service.get_batch(batch_id, current_user)


@router.patch("/{batch_id}", response_model=BatchOut)
async def update_batch(
    batch_id: UUID,
    data: BatchUpdate,
    teacher: UserOut = Depends(require_teacher)
):
    """
    Teacher updates batch name or description.
    """
    return await service.update_batch(batch_id, teacher, data)


@router.delete("/{batch_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_batch(
    batch_id: UUID,
    teacher: UserOut = Depends(require_teacher)
):
    """
    Teacher deletes a batch and all its enrollments.
    """
    await service.delete_batch(batch_id, teacher)


# ---------------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------------

@router.post("/join", response_model=EnrollmentOut, status_code=status.HTTP_201_CREATED)
async def join_batch(
    body: JoinBatchRequest,
    current_user: UserOut = Depends(get_current_user)
):
    """
    Student self-enrolls by submitting a batch_code.
    Returns the new enrollment record.
    """
    return await service.join_batch_by_code(current_user, body.batch_code)


@router.post("/{batch_id}/students/{student_id}", response_model=EnrollmentOut, status_code=status.HTTP_201_CREATED)
async def enroll_student(
    batch_id: UUID,
    student_id: UUID,
    teacher: UserOut = Depends(require_teacher)
):
    """
    Teacher manually enrolls a student into their batch.
    """
    return await service.enroll_student_manual(batch_id, student_id, teacher)


@router.delete("/{batch_id}/students/{student_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_student(
    batch_id: UUID,
    student_id: UUID,
    teacher: UserOut = Depends(require_teacher)
):
    """
    Teacher removes a student from their batch.
    """
    await service.remove_student(batch_id, student_id, teacher)


@router.get("/{batch_id}/students", response_model=List[dict])
async def get_students(
    batch_id: UUID,
    teacher: UserOut = Depends(require_teacher)
):
    """
    Teacher lists all enrolled students in a batch with enrollment metadata.
    """
    return await service.get_enrolled_students(batch_id, teacher)