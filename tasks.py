import os
import csv
from datetime import datetime, timedelta
from app import celery, db, Trek, Booking, User, Role
from celery.schedules import crontab

@celery.task(name='tasks.async_export_user_history')
def async_export_user_history(user_id):
    """
    User Triggered Async Job: Export user booking history as CSV 
    and alert once finished.
    """
    user = User.query.get(user_id)
    if not user:
        return f"User {user_id} not found"
    
    bookings = Booking.query.filter_by(user_id=user_id).all()
    filename = f"booking_history_user_{user_id}.csv"
    filepath = os.path.join(os.getcwd(), filename)
    
    with open(filepath, mode='w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['User ID', 'Trek Name', 'Location', 'Booking Status', 'Dates'])
        for b in bookings:
            writer.writerow([user_id, b.trek.name, b.trek.location, b.status, b.trek.start_date])
            
    # Alert simulation / notification log
    print(f"[ALERT] CSV Export completed for {user.email}. Saved to {filepath}")
    return f"Exported successfully to {filepath}"


@celery.task(name='tasks.send_daily_trek_reminders')
def send_daily_trek_reminders():
    """
    Scheduled Daily Reminders: Sends reminders to users regarding upcoming treks
    starting tomorrow (includes start date and instructions).
    """
    tomorrow = (datetime.utcnow() + timedelta(days=1)).strftime('%Y-%m-%d')
    upcoming_treks = Trek.query.filter_by(start_date=tomorrow, status='Open').all()
    
    if not upcoming_treks:
        return "No treks starting tomorrow."

    count = 0
    for trek in upcoming_treks:
        bookings = Booking.query.filter_by(trek_id=trek.id, status='Booked').all()
        for booking in bookings:
            user = booking.user
            reminder_msg = (
                f"Hello {user.name}, reminder that your trek '{trek.name}' at {trek.location} "
                f"starts tomorrow ({trek.start_date}). Please arrive on time with your gear."
            )
            print(f"[DAILY REMINDER SENT TO {user.email}]: {reminder_msg}")
            count += 1
            
    return f"Sent {count} daily trek reminders for upcoming trips."


@celery.task(name='tasks.send_monthly_admin_report')
def send_monthly_admin_report():
    """
    Scheduled Monthly Activity Report: Generates a monthly HTML report for the Admin
    detailing treks conducted, participants, and popular treks, sent on the 1st of every month.
    """
    total_treks = Trek.query.count()
    total_bookings = Booking.query.count()
    
    user_role = Role.query.filter_by(name='user').first()
    total_users = user_role.users.count() if user_role else 0

    treks = Trek.query.all()
    trek_summary = []
    for t in treks:
        b_count = Booking.query.filter_by(trek_id=t.id).count()
        trek_summary.append({'name': t.name, 'location': t.location, 'bookings': b_count})
    
    trek_summary = sorted(trek_summary, key=lambda x: x['bookings'], reverse=True)

    html_content = f"""
    <html>
    <head>
        <style>
            body {{ font-family: Arial, sans-serif; color: #333; }}
            .container {{ padding: 20px; border: 1px solid #ddd; border-radius: 8px; }}
            h2 {{ color: #0d6efd; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 15px; }}
            th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
            th {{ background-color: #f4f6f9; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h2>🏔️ Monthly Trekking Activity Report</h2>
            <p>Generated automatically on the 1st of the month.</p>
            <ul>
                <li><strong>Total Treks Conducted:</strong> {total_treks}</li>
                <li><strong>Total Participants / Bookings:</strong> {total_bookings}</li>
                <li><strong>Registered Users:</strong> {total_users}</li>
            </ul>
            <h3>Popular Treks Breakdown</h3>
            <table>
                <tr>
                    <th>Trek Name</th>
                    <th>Location</th>
                    <th>Total Bookings</th>
                </tr>
    """
    for ts in trek_summary[:5]:  # Top 5 popular treks
        html_content += f"<tr><td>{ts['name']}</td><td>{ts['location']}</td><td>{ts['bookings']}</td></tr>"

    html_content += """
            </table>
        </div>
    </body>
    </html>
    """

    admin_role = Role.query.filter_by(name='admin').first()
    admin_email = admin_role.users.first().email if admin_role and admin_role.users.first() else "admin@trek.com"

    print(f"[MONTHLY REPORT EMAIL SENT TO ADMIN ({admin_email})]:\n{html_content}")
    return f"Monthly activity report successfully sent to {admin_email}"

celery.conf.beat_schedule = {
    'send-daily-reminders-every-day': {
        'task': 'tasks.send_daily_trek_reminders',
        'schedule': crontab(hour=8, minute=0),  # Runs daily at 8:00 AM
    },
    'send-monthly-admin-report-first-day': {
        'task': 'tasks.send_monthly_admin_report',
        'schedule': crontab(day_of_month=1, hour=9, minute=0),  # Runs on the 1st of every month at 9:00 AM
    },
}
celery.conf.timezone = 'UTC'