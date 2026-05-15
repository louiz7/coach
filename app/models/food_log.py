import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, Text, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from app.database import Base


class FoodLogEntry(Base):
    __tablename__ = "food_log_entries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    # CDN URL of the image delivered by Linq webhook (type: "media")
    image_url = Column(Text, nullable=True)
    # LLM-generated plain-English description of what's on the plate
    description = Column(Text, nullable=True)
    estimated_calories = Column(Integer, nullable=True)
    # JSON array: [{"name": str, "calories": int}, ...]
    items_json = Column(Text, nullable=True)
    meal_type = Column(String(20), nullable=True)  # breakfast/lunch/dinner/snack/unknown
    recorded_at = Column(DateTime, default=datetime.utcnow)
