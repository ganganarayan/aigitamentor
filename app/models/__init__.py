"""ORM models. Importing this package registers every table on Base.metadata.

The normalized knowledge-object graph (Section 4 of the BuildSpec):
  knowledge   — concepts, chapters, verses, questions, topics, relationships
  corpus      — kb_sources, kb_answers, kb_chunks (System B, gated)
  public_kb   — public_kb_articles (System A, crawlable)
  mentor      — conversations, messages, generations, usage_counters
  accounts    — users, subscriptions, payments
  acquisition — contacts, events, webhooks, promotions, onboarding, help, settings
  ai_config   — ai_config (versioned)
  operational — recordings, llm_baselines
"""

from __future__ import annotations

from app.models.base import Base, PkMixin, TimestampMixin
from app.models.knowledge import (
    Chapter,
    Concept,
    Question,
    Relationship,
    Topic,
    Verse,
)
from app.models.corpus import EMBED_DIM, TIER_RANK, KbAnswer, KbChunk, KbSource, tier_level
from app.models.public_kb import PublicKbArticle
from app.models.mentor import (
    Conversation,
    ConversationSummary,
    Generation,
    Message,
    UsageCounter,
    UserPattern,
)
from app.models.accounts import Payment, Subscription, User
from app.models.acquisition import (
    Contact,
    Event,
    HelpArticle,
    OnboardingStage,
    Promotion,
    Setting,
    Webhook,
    WebhookLog,
)
from app.models.ai_config import AiConfig
from app.models.operational import Expense, LlmBaseline, Recording
from app.models.escalation import EscalationState, ResourceGrant, VideoResource

__all__ = [
    "Base",
    "PkMixin",
    "TimestampMixin",
    "TIER_RANK",
    "EMBED_DIM",
    "tier_level",
    # knowledge
    "Concept",
    "Chapter",
    "Verse",
    "Question",
    "Topic",
    "Relationship",
    # corpus
    "KbSource",
    "KbAnswer",
    "KbChunk",
    # public kb
    "PublicKbArticle",
    # mentor
    "Conversation",
    "ConversationSummary",
    "Message",
    "Generation",
    "UsageCounter",
    "UserPattern",
    # accounts
    "User",
    "Subscription",
    "Payment",
    # acquisition
    "Contact",
    "Event",
    "Webhook",
    "WebhookLog",
    "Promotion",
    "OnboardingStage",
    "HelpArticle",
    "Setting",
    # ai config
    "AiConfig",
    # operational
    "Recording",
    "LlmBaseline",
    "Expense",
    # escalation (Chunk 5)
    "VideoResource",
    "ResourceGrant",
    "EscalationState",
]
