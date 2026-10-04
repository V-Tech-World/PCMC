import smtplib
from email.mime.text import MIMEText

def send_email_alert(to_email: str, subject: str, body: str):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = "SENDER_EMAIL"  # Replace with your email address
    msg["To"] = to_email

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login("SENDER_EMAIL", "GOOGLE_APP_PASSWORD")  # from .env
        server.send_message(msg)

send_email_alert(
    to_email="example@example.com",  # Replace with the recipient's email address for the alert
    subject="NEW PATIENT ALERT: A new patient Needs Attention",
    body="A new patient has been added to the system and requires attention. Please check the dashboard for details.", # and more details about the call and the responses
)