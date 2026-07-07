import logging

from fastapi import APIRouter, Depends, HTTPException, status

from selfai_ui.models.benchmark_config import BenchmarkConfigs, BenchmarkConfigModel, BenchmarkConfigUpdate
from selfai_ui.utils.auth import get_admin_user

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("", response_model=list[BenchmarkConfigModel])
async def list_benchmarks(user=Depends(get_admin_user)):
    return BenchmarkConfigs.get_all()


@router.put("/{id}", response_model=BenchmarkConfigModel)
async def update_benchmark(id: str, form: BenchmarkConfigUpdate, user=Depends(get_admin_user)):
    result = BenchmarkConfigs.update(id=id, update=form)
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Benchmark not found")
    return result
