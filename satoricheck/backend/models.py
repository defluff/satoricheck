"""
Database models for Authenix.
"""
from datetime import datetime, UTC
import json
from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, Text, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()


class User(Base):
    """User account model."""
    __tablename__ = 'users'
    
    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    api_token = Column(String(64), unique=True, index=True)  # Secure API token for Bearer auth
    created_at = Column(DateTime, default=lambda: datetime.now(UTC))
    last_login = Column(DateTime)
    

    # Volatile Cache Tracking (Clears on new uploads to prevent bloat)
    current_media_cache = Column(String(255))
    current_pitchdeck_cache = Column(String(255))
    
    # Relationships
    token_balance = relationship('TokenBalance', back_populates='user', uselist=False)
    streak = relationship('Streak', back_populates='user', uselist=False)
    transactions = relationship('Transaction', back_populates='user')
    fact_checks = relationship('FactCheck', back_populates='user')
    media_checks = relationship('MediaCheck', back_populates='user')
    cloud_connections = relationship('CloudStorageConnection', back_populates='user')
    synced_deck_records = relationship('SyncedDeckRecord', back_populates='user')
    
    def __repr__(self):
        return f'<User id={self.id}>'


class TokenBalance(Base):
    """User token balance (CP - Check Points)."""
    __tablename__ = 'token_balances'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), unique=True, nullable=False)
    balance = Column(Integer, default=0)
    last_updated = Column(DateTime, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))
    
    # Wizard subscription tracking
    is_wizard = Column(Boolean, default=False)
    wizard_start_date = Column(DateTime)
    wizard_months_remaining = Column(Integer, default=0)
    
    # Legacy column retained for Cloud SQL PostgreSQL schema compatibility (word-accumulation billing retired)
    unbilled_words = Column(Integer, default=0)

    # Automated off-session CP refill configuration
    auto_refill_enabled = Column(Boolean, default=False)
    auto_refill_package = Column(String(32), nullable=True)  # 'battery_medium' or 'battery_large'
    auto_refill_threshold = Column(Integer, default=0)
    stripe_customer_id = Column(String(128), nullable=True)
    stripe_payment_method_id = Column(String(128), nullable=True)
    auto_refill_failed = Column(Boolean, default=False)
    
    user = relationship('User', back_populates='token_balance')
    
    def __repr__(self):
        return f'<TokenBalance user_id={self.user_id} balance={self.balance}>'


class Streak(Base):
    """User daily login streak."""
    __tablename__ = 'streaks'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), unique=True, nullable=False)
    current_streak = Column(Integer, default=0)
    longest_streak = Column(Integer, default=0)
    cycle_reward_high_water = Column(Integer, default=0)
    last_active_date = Column(DateTime)
    
    user = relationship('User', back_populates='streak')
    
    def __repr__(self):
        return f'<Streak user_id={self.user_id} current={self.current_streak}>'


class Transaction(Base):
    """Transaction history for token purchases."""
    __tablename__ = 'transactions'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    
    # Transaction details
    type = Column(String(50), nullable=False)  # 'purchase', 'bonus', 'deduction', 'refund'
    amount = Column(Integer, nullable=False)  # positive for credits, negative for deductions
    description = Column(String(255))
    
    # Stripe details
    stripe_session_id = Column(String(255))
    stripe_customer_id = Column(String(255))
    package_type = Column(String(50))
    
    timestamp = Column(DateTime, default=lambda: datetime.now(UTC))
    
    user = relationship('User', back_populates='transactions')
    
    def __repr__(self):
        return f'<Transaction user_id={self.user_id} type={self.type} amount={self.amount}>'

    def to_dict(self) -> dict:
        """Serialize transaction to a JSON-friendly dict."""
        return {
            'id': self.id,
            'type': self.type,
            'amount': self.amount,
            'description': self.description,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
        }


class FactCheck(Base):
    """Fact-check history."""
    __tablename__ = 'fact_checks'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    
    # Claim details
    claim_text = Column(Text, nullable=False)
    word_count = Column(Integer)
    tokens_used = Column(Integer)
    
    # Analysis results
    is_claim = Column(Boolean)
    verdict = Column(String(50))  # 'TRUE', 'FALSE', 'MISLEADING', 'NOT_A_CLAIM'
    explanation = Column(Text)
    fallacy = Column(String(255))
    sources = Column(Text)  # JSON array of URLs
    source_reliability = Column(String(20))  # 'HIGH', 'MEDIUM', 'LOW'
    
    # Metadata for filtering and session tracking
    source = Column(String(50), default='factcheck', index=True)  # 'factcheck', 'pitchdeck'
    source_id = Column(String(100), index=True)  # Links to a session or document ID
    
    # Metadata
    timestamp = Column(DateTime, default=lambda: datetime.now(UTC), index=True)
    processing_time = Column(Float)  # in seconds
    
    user = relationship('User', back_populates='fact_checks')
    
    # Composite index for efficient history queries
    __table_args__ = (
        Index('ix_factcheck_user_source_timestamp', 'user_id', 'source', 'timestamp'),
    )
    
    def __repr__(self):
        return f'<FactCheck id={self.id} verdict={self.verdict}>'

    def to_dict(self) -> dict:
        """Serialize fact-check to a JSON-friendly dict."""
        return {
            'id': self.id,
            'claim_text': self.claim_text,
            'verdict': self.verdict,
            'explanation': self.explanation,
            'fallacy': self.fallacy,
            'sources': json.loads(self.sources) if self.sources else [],
            'source_reliability': self.source_reliability,
            'tokens_used': self.tokens_used,
            'source': self.source,
            'source_id': self.source_id,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
        }



class DeletedUser(Base):
    """Minimal record to prevent bonus abuse and track churn."""
    __tablename__ = 'deleted_users'
    
    id = Column(Integer, primary_key=True)
    email_hash = Column(String(64), unique=True, index=True)  # SHA256 of email
    deleted_at = Column(DateTime, default=lambda: datetime.now(UTC))
    
    def __repr__(self):
        return f'<DeletedUser hash={self.email_hash[:10]}...>'


class ShareStats(Base):
    """Anonymous share tracking for social sharing feature.
    
    Privacy-first: No user_id, no content stored.
    Only tracks platform and timestamp for aggregate analytics.
    """
    __tablename__ = 'share_stats'
    
    id = Column(Integer, primary_key=True)
    platform = Column(String(20), nullable=False)  # 'X', 'LinkedIn', 'Download'
    created_at = Column(DateTime, default=lambda: datetime.now(UTC), index=True)
    
    def __repr__(self):
        return f'<ShareStats platform={self.platform}>'



class MediaCheck(Base):
    """Media authenticity check history."""
    __tablename__ = 'media_checks'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    
    # Input
    url = Column(Text, nullable=False)
    mime_type = Column(String(100))
    
    # Analysis results
    verdict = Column(String(50))  # 'ai', 'authentic', 'cgi', 'camera_trick', 'inconclusive'
    confidence = Column(Integer)
    reasoning = Column(Text)
    criteria_json = Column(Text)  # JSON storage for detailed criteria
    claims_json = Column(Text, nullable=True)  # JSON storage for extracted spoken claims with timestamps
    
    # Fingerprinting
    embedding_json = Column(Text)  # JSON storage for multimodal embedding vector
    
    # Metadata
    timestamp = Column(DateTime, default=lambda: datetime.now(UTC), index=True)
    processing_time = Column(Float)
    
    user = relationship('User', back_populates='media_checks')
    
    # Index for fast history lookup
    __table_args__ = (
        Index('ix_mediacheck_user_timestamp', 'user_id', 'timestamp'),
    )
    
    def __repr__(self):
        return f'<MediaCheck id={self.id} verdict={self.verdict}>'


class AuditLog(Base):
    """Security and OAuth audit event log (strictly non-PII)."""
    __tablename__ = 'audit_logs'

    id = Column(Integer, primary_key=True)
    # No FK to users.id so account deletion audit trails survive GDPR user erasure.
    user_id = Column(Integer, nullable=True, index=True)
    event_type = Column(String(64), nullable=False, index=True)
    provider = Column(String(32), nullable=True)
    detail = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(UTC), nullable=False, index=True)

    __table_args__ = (
        Index('ix_auditlog_user_created', 'user_id', 'created_at'),
    )

    def __repr__(self):
        return f'<AuditLog id={self.id} event_type={self.event_type}>'


def record_audit_event(
    session_db,
    event_type: str,
    *,
    user_id: int | None = None,
    provider: str | None = None,
    detail: str | None = None,
    commit: bool = False,
) -> AuditLog:
    """Append a strictly non-PII AuditLog entry to the given SQLAlchemy session."""
    safe_event_type = str(event_type)[:64]
    safe_provider = str(provider)[:32] if provider is not None else None
    safe_detail = str(detail)[:255] if detail is not None else None
    entry = AuditLog(
        user_id=user_id,
        event_type=safe_event_type,
        provider=safe_provider,
        detail=safe_detail,
        created_at=datetime.now(UTC),
    )
    session_db.add(entry)
    if commit:
        session_db.commit()
    return entry


class CloudStorageConnection(Base):
    """Per-user encrypted OAuth connection and sync config for a cloud storage provider."""
    __tablename__ = 'cloud_storage_connections'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    provider = Column(String(32), nullable=False)  # 'google_drive', 'dropbox', 'onedrive'

    encrypted_access_token = Column(Text, nullable=True)
    encrypted_refresh_token = Column(Text, nullable=True)
    token_expiry = Column(DateTime, nullable=True)
    granted_scopes = Column(Text, nullable=True)

    input_folder_ref = Column(String(255), nullable=True)
    output_folder_ref = Column(String(255), nullable=True)
    daily_sync_enabled = Column(Boolean, default=False, nullable=False)
    memo_preset = Column(String(16), default='brief', nullable=False)

    auto_export_sheets = Column(Boolean, default=False, nullable=False)
    sheets_spreadsheet_id = Column(String(128), nullable=True)
    sheets_sheet_name = Column(String(128), nullable=True)

    last_synced_at = Column(DateTime, nullable=True)
    last_error = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(UTC), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC), nullable=False)

    user = relationship('User', back_populates='cloud_connections')
    synced_decks = relationship('SyncedDeckRecord', back_populates='connection')

    __table_args__ = (
        UniqueConstraint('user_id', 'provider', name='uq_cloud_storage_user_provider'),
    )

    def __repr__(self):
        return f'<CloudStorageConnection id={self.id} user_id={self.user_id} provider={self.provider}>'

    @staticmethod
    def default_status_dict(provider: str) -> dict:
        """Return status defaults for an unconfigured provider (strictly token-free)."""
        return {
            'provider': provider,
            'connected': False,
            'input_folder_ref': None,
            'output_folder_ref': None,
            'daily_sync_enabled': False,
            'memo_preset': 'brief',
            'auto_export_sheets': False,
            'sheets_spreadsheet_id': None,
            'sheets_sheet_name': None,
            'last_synced_at': None,
            'last_error': None,
        }

    def to_status_dict(self) -> dict:
        """Serialize connection status without exposing any encrypted/plaintext tokens."""
        return {
            'provider': self.provider,
            'connected': bool(self.encrypted_access_token or self.encrypted_refresh_token),
            'input_folder_ref': self.input_folder_ref,
            'output_folder_ref': self.output_folder_ref,
            'daily_sync_enabled': bool(self.daily_sync_enabled),
            'memo_preset': self.memo_preset or 'brief',
            'auto_export_sheets': bool(self.auto_export_sheets),
            'sheets_spreadsheet_id': self.sheets_spreadsheet_id,
            'sheets_sheet_name': self.sheets_sheet_name,
            'last_synced_at': self.last_synced_at.isoformat() if self.last_synced_at else None,
            'last_error': self.last_error,
        }


class SyncedDeckRecord(Base):
    """Deduplication and audit ledger for cloud-synced pitch decks."""
    __tablename__ = 'synced_deck_records'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    connection_id = Column(Integer, ForeignKey('cloud_storage_connections.id'), nullable=False, index=True)

    external_file_id = Column(String(255), nullable=False)
    file_name = Column(String(255), nullable=False)
    file_etag_or_modified = Column(String(255), nullable=False)
    status = Column(String(32), nullable=False)  # 'completed', 'insufficient_cp', 'skipped_invalid', 'error'

    exported_doc_url = Column(String(512), nullable=True)
    exported_sheet_range = Column(String(128), nullable=True)
    cp_charged = Column(Integer, default=0, nullable=False)
    error_message = Column(String(255), nullable=True)
    processed_at = Column(DateTime, default=lambda: datetime.now(UTC), nullable=False)

    user = relationship('User', back_populates='synced_deck_records')
    connection = relationship('CloudStorageConnection', back_populates='synced_decks')

    __table_args__ = (
        Index('ix_synced_deck_dedup', 'connection_id', 'external_file_id', 'file_etag_or_modified'),
    )

    def __repr__(self):
        return f'<SyncedDeckRecord id={self.id} connection_id={self.connection_id} status={self.status}>'


