"""
SatoriCheck Admin CLI
Use this script to manually manage users and token balances.

Usage:
    python manage.py list-users
    python manage.py set-balance <email> <amount>
    python manage.py add-tokens <email> <amount>

Examples:
    python manage.py list-users
    python manage.py set-balance andy@kiniroo.com 5000
    python manage.py add-tokens andy@kiniroo.com 100
"""
import sys
import argparse
from backend.server import app
from backend.database import db_session
from backend.models import User, TokenBalance, Transaction

def list_users():
    """List all users and their balances."""
    with app.app_context():
        users = db_session.query(User).all()
        print(f"\n{'ID':<5} {'Email':<30} {'Balance':<10} {'Streak':<5}")
        print("-" * 55)
        for u in users:
            bal = u.token_balance.balance if u.token_balance else 0
            streak = u.streak.current_streak if u.streak else 0
            print(f"{u.id:<5} {u.email:<30} {bal:<10} {streak:<5}")
        print("-" * 55)
        print(f"Total Users: {len(users)}\n")

def get_user(email):
    return db_session.query(User).filter_by(email=email).first()

def set_balance(email, amount):
    """Set a user's absolute balance."""
    with app.app_context():
        user = get_user(email)
        if not user:
            print(f"❌ User not found: {email}")
            return

        if not user.token_balance:
            print(f"⚠️  No balance record found for {email}, creating one...")
            user.token_balance = TokenBalance(user_id=user.id, balance=0)
            db_session.add(user.token_balance)

        old_bal = user.token_balance.balance
        user.token_balance.balance = int(amount)
        
        # Log transaction
        tx = Transaction(
            user_id=user.id,
            type='admin_adjustment',
            amount=int(amount) - old_bal,
            description=f'Admin manual set: {old_bal} -> {amount}'
        )
        db_session.add(tx)
        
        db_session.commit()
        print(f"✅ Updated {email}: {old_bal} CP -> {amount} CP")

def add_tokens(email, amount):
    """Add tokens to existing balance."""
    with app.app_context():
        user = get_user(email)
        if not user:
            print(f"❌ User not found: {email}")
            return

        if not user.token_balance:
            user.token_balance = TokenBalance(user_id=user.id, balance=0)
            db_session.add(user.token_balance)

        old_bal = user.token_balance.balance
        user.token_balance.balance += int(amount)
        
        # Log transaction
        tx = Transaction(
            user_id=user.id,
            type='admin_grant',
            amount=int(amount),
            description=f'Admin grant: +{amount}'
        )
        db_session.add(tx)
        
        db_session.commit()
        print(f"✅ Added {amount} CP to {email}. New Balance: {user.token_balance.balance}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SatoriCheck Admin CLI")
    subparsers = parser.add_subparsers(dest='command')

    # list-users
    subparsers.add_parser('list-users', help='List all registered users')

    # set-balance
    wb_parser = subparsers.add_parser('set-balance', help='Set absolute CP balance')
    wb_parser.add_argument('email', help='User email')
    wb_parser.add_argument('amount', type=int, help='New CP amount')

    # add-tokens
    at_parser = subparsers.add_parser('add-tokens', help='Add tokens to existing balance')
    at_parser.add_argument('email', help='User email')
    at_parser.add_argument('amount', type=int, help='Amount to add')

    args = parser.parse_args()

    if args.command == 'list-users':
        list_users()
    elif args.command == 'set-balance':
        set_balance(args.email, args.amount)
    elif args.command == 'add-tokens':
        add_tokens(args.email, args.amount)
    else:
        parser.print_help()
