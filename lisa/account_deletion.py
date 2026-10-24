"""Remove an account through the existing account lock and database cascades."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, text

from lisa.auth import COOKIE, DB, current_user
from lisa.models import (
    Account,
    BudgetDay,
    GuideNotice,
    ModelCall,
    QQBinding,
    ServiceCode,
    ServiceEvent,
)
from lisa.security import verify_password
from lisa.works import lock_account

router = APIRouter(tags=["account"])


class DeleteAccount(BaseModel):
    password: str = Field(min_length=10, max_length=128)


@router.delete("/api/auth/account", status_code=204)
def delete_account(
    body: DeleteAccount,
    request: Request,
    response: Response,
    db: DB,
    user: Annotated[Account, Depends(current_user)],
):
    # Same ordering as model reservation/settlement; external calls never hold this lock.
    db.execute(text("SELECT pg_advisory_xact_lock(7273101)"))
    user = lock_account(db, user.id)
    if user is None or not user.active:
        raise HTTPException(401, "账号已退出或删除，请重新登录。")
    if not verify_password(body.password, user.password_hash):
        raise HTTPException(403, "当前密码不正确，账号尚未删除。")
    user.active = False
    db.flush()

    # An in-flight provider outcome is not a refund. Retain only anonymous daily totals.
    for call in db.scalars(
        select(ModelCall).where(ModelCall.user_id == user.id, ModelCall.state == "reserved")
    ):
        day = db.scalar(select(BudgetDay).where(BudgetDay.day == call.budget_day).with_for_update())
        day.reserved_usd -= call.reserved_usd
        day.spent_usd += call.reserved_usd

    # Let a running worker acknowledge its job, without retaining the account/task linkage.
    # Aborting also prevents an already-prepared retry from reviving that job.
    job_ids = list(
        db.scalars(
            text(
                "SELECT id FROM procrastinate_jobs WHERE args->>'task_id' IN "
                "(SELECT id::text FROM tasks WHERE user_id = :user_id) FOR UPDATE"
            ),
            {"user_id": user.id},
        )
    )
    for job_id in job_ids:
        db.execute(
            text(
                "UPDATE procrastinate_jobs SET args = '{}'::jsonb, lock = NULL, "
                "queueing_lock = NULL, abort_requested = true WHERE id = :id AND status = 'doing'"
            ),
            {"id": job_id},
        )
        db.execute(text("DELETE FROM procrastinate_events WHERE job_id = :id"), {"id": job_id})
        db.execute(
            text("DELETE FROM procrastinate_jobs WHERE id = :id AND status <> 'doing'"),
            {"id": job_id},
        )

    binding = db.get(QQBinding, user.id)
    if binding:
        for model in (ServiceCode, ServiceEvent, GuideNotice):
            db.execute(delete(model).where(model.qq_id == binding.qq_id))
    from lisa.shared import cleanup_account_contributions
    cleanup_account_contributions(db, user.id)
    db.delete(user)
    db.commit()
    response.delete_cookie(
        COOKIE,
        httponly=True,
        secure=request.app.state.settings.cookie_secure,
        samesite="strict",
        path="/",
    )
