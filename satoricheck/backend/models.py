"""
Database models for SatoriCheck.
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, Text, ForeignKey, Index
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship

Base = declarative_base()


class User(Base):
    """User account model."""
    __tablename__ = 'users'
    
    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    api_token = Column(String(64), unique=True, index=True)  # Secure API token for Bearer auth
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login = Column(DateTime)
    
    # Relationships
    token_balance = relationship('TokenBalance', back_populates='user', uselist=False)
    streak = relationship('Streak', back_populates='user', uselist=False)
    transactions = relationship('Transaction', back_populates='user')
    fact_checks = relationship('FactCheck', back_populates='user')
    
    def __repr__(self):
        return f'<User {self.email}>'


class TokenBalance(Base):
    """User token balance (CP - Check Points)."""
    __tablename__ = 'token_balances'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), unique=True, nullable=False)
    balance = Column(Integer, default=0)
    last_updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Wizard subscription tracking
    is_wizard = Column(Boolean, default=False)
    wizard_start_date = Column(DateTime)
    wizard_months_remaining = Column(Integer, default=0)
    
    # Usage tracking for fairness (1 CP / 250 words)
    unbilled_words = Column(Integer, default=0)
    
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
    
    timestamp = Column(DateTime, default=datetime.utcnow)
    
    user = relationship('User', back_populates='transactions')
    
    def __repr__(self):
        return f'<Transaction user_id={self.user_id} type={self.type} amount={self.amount}>'


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
    
    # Metadata
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    processing_time = Column(Float)  # in seconds
    
    user = relationship('User', back_populates='fact_checks')
    
    # Composite index for efficient history queries
    __table_args__ = (
        Index('ix_factcheck_user_timestamp', 'user_id', 'timestamp'),
    )
    
    def __repr__(self):
        return f'<FactCheck id={self.id} verdict={self.verdict}>'

