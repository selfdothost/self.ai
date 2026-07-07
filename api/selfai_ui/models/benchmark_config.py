import logging
import time
from typing import Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Column, Integer, Text

from selfai_ui.internal.db import Base, get_db
from selfai_ui.env import SRC_LOG_LEVELS

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])


####################
# BenchmarkConfig DB Schema
####################


class BenchmarkConfig(Base):
    __tablename__ = "benchmark_config"

    id = Column(Text, unique=True, primary_key=True)
    benchmark = Column(Text)
    eval_type = Column(Text)
    max_duration_minutes = Column(Integer, default=120)
    notes = Column(Text, nullable=True)
    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)


class BenchmarkConfigModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    benchmark: str
    eval_type: str
    max_duration_minutes: int = 120
    notes: Optional[str] = None
    created_at: int
    updated_at: int


class BenchmarkConfigUpdate(BaseModel):
    max_duration_minutes: int
    notes: Optional[str] = None


####################
# Table CRUD Class
####################


class BenchmarkConfigTable:
    def get_all(self) -> list[BenchmarkConfigModel]:
        with get_db() as db:
            rows = (
                db.query(BenchmarkConfig)
                .order_by(BenchmarkConfig.eval_type, BenchmarkConfig.benchmark)
                .all()
            )
            return [BenchmarkConfigModel.model_validate(r) for r in rows]

    def get_by_id(self, id: str) -> Optional[BenchmarkConfigModel]:
        try:
            with get_db() as db:
                row = db.query(BenchmarkConfig).filter_by(id=id).first()
                return BenchmarkConfigModel.model_validate(row) if row else None
        except Exception:
            return None

    def get_by_benchmark(self, benchmark: str, eval_type: str) -> Optional[BenchmarkConfigModel]:
        try:
            with get_db() as db:
                row = (
                    db.query(BenchmarkConfig)
                    .filter_by(benchmark=benchmark, eval_type=eval_type)
                    .first()
                )
                return BenchmarkConfigModel.model_validate(row) if row else None
        except Exception:
            return None

    def update(self, id: str, update: BenchmarkConfigUpdate) -> Optional[BenchmarkConfigModel]:
        try:
            with get_db() as db:
                fields = {
                    "max_duration_minutes": update.max_duration_minutes,
                    "notes": update.notes,
                    "updated_at": int(time.time()),
                }
                db.query(BenchmarkConfig).filter_by(id=id).update(fields)
                db.commit()
                return self.get_by_id(id=id)
        except Exception as e:
            log.exception(e)
            return None


BenchmarkConfigs = BenchmarkConfigTable()
