import asyncio
import imaplib
import smtplib
import email
import os
import logging
import re
import requests
import json
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from typing import Any, List, Optional, Dict, Tuple
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse
import time

from mcp.server.fastmcp import FastMCP
from mcp.types import Resource, Tool, TextContent
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# MCP Server instance
mcp = FastMCP("ProtonEmailServer")

class ProtonEmailClient:
    def __init__(self):
        self.imap_host = os.getenv("BRIDGE_IMAP_HOST", "127.0.0.1")
        self.imap_port = int(os.getenv("BRIDGE_IMAP_PORT", "1143"))
        self.smtp_host = os.getenv("BRIDGE_SMTP_HOST", "127.0.0.1")
        self.smtp_port = int(os.getenv("BRIDGE_SMTP_PORT", "1025"))
        self.email = os.getenv("PROTON_EMAIL")
        self.password = os.getenv("PROTON_BRIDGE_PASSWORD")
        self.rules_file = os.path.join(os.path.dirname(__file__), "filter_rules.json")
        
        if not all([self.email, self.password]):
            raise ValueError("Email credentials not found in environment variables")
    
    def connect_imap(self):
        """Connect to IMAP server"""
        try:
            mail = imaplib.IMAP4(self.imap_host, self.imap_port)
            mail.login(self.email, self.password)
            return mail
        except Exception as e:
            logger.error(f"IMAP connection failed: {e}")
            raise
    
    def connect_smtp(self):
        """Connect to SMTP server"""
        try:
            server = smtplib.SMTP(self.smtp_host, self.smtp_port)
            server.starttls()
            server.login(self.email, self.password)
            return server
        except Exception as e:
            logger.error(f"SMTP connection failed: {e}")
            raise
    
    def decode_mime_words(self, text):
        """Decode MIME encoded words"""
        if text is None:
            return ""
        decoded_words = decode_header(text)
        decoded_text = ""
        for word, encoding in decoded_words:
            if isinstance(word, bytes):
                word = word.decode(encoding or 'utf-8', errors='ignore')
            decoded_text += word
        return decoded_text
    
    def search_emails(self, query: str = "ALL", mailbox: str = "INBOX", limit: int = 10):
        """Search emails in specified mailbox"""
        mail = self.connect_imap()
        mailbox_selected = False
        try:
            status, response = mail.select(mailbox)
            if status != 'OK':
                logger.error(f"Failed to select mailbox '{mailbox}': {response}")
                return []
            mailbox_selected = True
            
            status, messages = mail.search(None, query)
            
            if status != 'OK':
                return []
            
            message_ids = messages[0].split()
            # Get most recent emails (reverse order)
            message_ids = message_ids[-limit:]
            
            emails = []
            for msg_id in reversed(message_ids):
                status, msg_data = mail.fetch(msg_id, '(RFC822)')
                if status == 'OK':
                    raw_email = msg_data[0][1]
                    email_message = email.message_from_bytes(raw_email)
                    
                    # Extract email details
                    subject = self.decode_mime_words(email_message['Subject'])
                    from_addr = self.decode_mime_words(email_message['From'])
                    date = email_message['Date']
                    
                    # Get email body
                    body = self.get_email_body(email_message)
                    
                    emails.append({
                        'id': msg_id.decode(),
                        'subject': subject,
                        'from': from_addr,
                        'date': date,
                        'body': body[:500] + "..." if len(body) > 500 else body  # Truncate for preview
                    })
            
            return emails
        finally:
            if mailbox_selected:
                mail.close()
            mail.logout()
    
    def get_email_body(self, email_message):
        """Extract email body text"""
        body = ""
        if email_message.is_multipart():
            for part in email_message.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition"))
                
                if content_type == "text/plain" and "attachment" not in content_disposition:
                    try:
                        body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                        break
                    except:
                        continue
        else:
            try:
                body = email_message.get_payload(decode=True).decode('utf-8', errors='ignore')
            except:
                body = str(email_message.get_payload())
        
        return body
    
    def get_full_email(self, email_id: str, mailbox: str = "INBOX"):
        """Get full email content by ID"""
        mail = self.connect_imap()
        mailbox_selected = False
        try:
            status, response = mail.select(mailbox)
            if status != 'OK':
                logger.error(f"Failed to select mailbox '{mailbox}': {response}")
                return None
            mailbox_selected = True
            
            status, msg_data = mail.fetch(email_id, '(RFC822)')
            
            if status != 'OK':
                return None
            
            raw_email = msg_data[0][1]
            email_message = email.message_from_bytes(raw_email)
            
            return {
                'id': email_id,
                'subject': self.decode_mime_words(email_message['Subject']),
                'from': self.decode_mime_words(email_message['From']),
                'to': self.decode_mime_words(email_message['To']),
                'date': email_message['Date'],
                'body': self.get_email_body(email_message)
            }
        finally:
            if mailbox_selected:
                mail.close()
            mail.logout()
    
    def get_bulk_emails(self, email_ids: List[str], mailbox: str = "INBOX", batch_size: int = 50):
        """
        Get multiple emails efficiently in batches using IMAP pipelining.
        
        Args:
            email_ids: List of email IDs to retrieve
            mailbox: Mailbox containing the emails
            batch_size: Number of emails to fetch per IMAP command
        
        Returns:
            Dictionary mapping email_id to email data
        """
        if not email_ids:
            return {}
        
        mail = self.connect_imap()
        emails = {}
        mailbox_selected = False
        
        try:
            status, response = mail.select(mailbox)
            if status != 'OK':
                logger.error(f"Failed to select mailbox '{mailbox}': {response}")
                return {}
            mailbox_selected = True
            
            # Process emails in batches for efficiency
            for i in range(0, len(email_ids), batch_size):
                batch_ids = email_ids[i:i + batch_size]
                
                # Create comma-separated list for IMAP FETCH
                id_list = ','.join(batch_ids)
                
                try:
                    status, msg_data = mail.fetch(id_list, '(RFC822)')
                    
                    if status == 'OK' and msg_data:
                        # Process each email in the batch
                        for j, item in enumerate(msg_data):
                            if isinstance(item, tuple) and len(item) >= 2:
                                raw_email = item[1]
                                if raw_email:
                                    email_message = email.message_from_bytes(raw_email)
                                    email_id = batch_ids[j // 2] if j % 2 == 1 else batch_ids[j]
                                    
                                    emails[email_id] = {
                                        'id': email_id,
                                        'subject': self.decode_mime_words(email_message['Subject']),
                                        'from': self.decode_mime_words(email_message['From']),
                                        'to': self.decode_mime_words(email_message['To']),
                                        'date': email_message['Date'],
                                        'body': self.get_email_body(email_message)
                                    }
                except Exception as e:
                    logger.warning(f"Failed to fetch batch {i//batch_size + 1}: {e}")
                    # Fallback to individual fetching for this batch
                    for email_id in batch_ids:
                        try:
                            status, msg_data = mail.fetch(email_id, '(RFC822)')
                            if status == 'OK' and msg_data and msg_data[0][1]:
                                raw_email = msg_data[0][1]
                                email_message = email.message_from_bytes(raw_email)
                                emails[email_id] = {
                                    'id': email_id,
                                    'subject': self.decode_mime_words(email_message['Subject']),
                                    'from': self.decode_mime_words(email_message['From']),
                                    'to': self.decode_mime_words(email_message['To']),
                                    'date': email_message['Date'],
                                    'body': self.get_email_body(email_message)
                                }
                        except Exception as inner_e:
                            logger.warning(f"Failed to fetch email {email_id}: {inner_e}")
                            continue
        
        finally:
            if mailbox_selected:
                mail.close()
            mail.logout()
        
        return emails
    
    def get_bulk_emails_with_html(self, email_ids: List[str], mailbox: str = "INBOX", batch_size: int = 30):
        """
        Get multiple emails with HTML content efficiently in batches.
        Used for unsubscribe link detection and other HTML analysis.
        """
        if not email_ids:
            return {}
        
        mail = self.connect_imap()
        emails = {}
        mailbox_selected = False
        
        try:
            status, response = mail.select(mailbox)
            if status != 'OK':
                logger.error(f"Failed to select mailbox '{mailbox}': {response}")
                return {}
            mailbox_selected = True
            
            # Process in smaller batches since HTML emails are larger
            for i in range(0, len(email_ids), batch_size):
                batch_ids = email_ids[i:i + batch_size]
                id_list = ','.join(batch_ids)
                
                try:
                    status, msg_data = mail.fetch(id_list, '(RFC822)')
                    
                    if status == 'OK' and msg_data:
                        for j, item in enumerate(msg_data):
                            if isinstance(item, tuple) and len(item) >= 2:
                                raw_email = item[1]
                                if raw_email:
                                    email_message = email.message_from_bytes(raw_email)
                                    email_id = batch_ids[j // 2] if j % 2 == 1 else batch_ids[j]
                                    
                                    # Extract both text and HTML content
                                    text_content = ""
                                    html_content = ""
                                    
                                    if email_message.is_multipart():
                                        for part in email_message.walk():
                                            content_type = part.get_content_type()
                                            content_disposition = str(part.get("Content-Disposition"))
                                            
                                            if "attachment" not in content_disposition:
                                                try:
                                                    payload = part.get_payload(decode=True)
                                                    if payload:
                                                        content = payload.decode('utf-8', errors='ignore')
                                                        if content_type == "text/plain":
                                                            text_content += content
                                                        elif content_type == "text/html":
                                                            html_content += content
                                                except:
                                                    continue
                                    else:
                                        try:
                                            payload = email_message.get_payload(decode=True)
                                            if payload:
                                                content = payload.decode('utf-8', errors='ignore')
                                                if email_message.get_content_type() == "text/html":
                                                    html_content = content
                                                else:
                                                    text_content = content
                                        except:
                                            pass
                                    
                                    emails[email_id] = {
                                        'id': email_id,
                                        'subject': self.decode_mime_words(email_message['Subject']),
                                        'from': self.decode_mime_words(email_message['From']),
                                        'to': self.decode_mime_words(email_message['To']),
                                        'date': email_message['Date'],
                                        'text_body': text_content,
                                        'html_body': html_content,
                                        'list_unsubscribe': email_message.get('List-Unsubscribe', ''),
                                        'list_unsubscribe_post': email_message.get('List-Unsubscribe-Post', '')
                                    }
                except Exception as e:
                    logger.warning(f"Failed to fetch HTML batch {i//batch_size + 1}: {e}")
                    # Fallback to individual fetching
                    for email_id in batch_ids:
                        individual_result = self.get_full_email_with_html(email_id, mailbox)
                        if individual_result:
                            emails[email_id] = individual_result
        
        finally:
            if mailbox_selected:
                mail.close()
            mail.logout()
        
        return emails
    
    def send_email(self, to: str, subject: str, body: str, reply_to_id: str = None):
        """Send email via SMTP"""
        server = self.connect_smtp()
        try:
            msg = MIMEMultipart()
            msg['From'] = self.email
            msg['To'] = to
            msg['Subject'] = subject
            
            # Add reply headers if replying to an email
            if reply_to_id:
                msg['In-Reply-To'] = reply_to_id
                msg['References'] = reply_to_id
            
            msg.attach(MIMEText(body, 'plain'))
            
            server.send_message(msg)
            return True
        except Exception as e:
            logger.error(f"Failed to send email: {e}")
            return False
        finally:
            server.quit()
    
    def is_junk_email(self, email_data: Dict) -> Dict[str, Any]:
        """
        Analyze email to determine if it's likely junk/spam.
        Returns a dictionary with junk detection results.
        """
        junk_indicators = []
        junk_score = 0
        
        subject = email_data.get('subject', '').lower()
        from_addr = email_data.get('from', '').lower()
        body = email_data.get('body', '').lower()
        
        # Common spam subject patterns
        spam_subject_patterns = [
            r'urgent.*action.*required',
            r'congratulations.*won',
            r'free.*money|money.*free',
            r'limited.*time.*offer',
            r'act.*now|click.*here',
            r'viagra|cialis|pharmacy',
            r'increase.*size',
            r'lose.*weight.*fast',
            r'make.*money.*fast',
            r'nigerian.*prince',
            r'tax.*refund',
            r'security.*alert',
            r're:.*re:.*re:',  # Multiple "re:" forwards
        ]
        
        for pattern in spam_subject_patterns:
            if re.search(pattern, subject):
                junk_indicators.append(f"Suspicious subject pattern: {pattern}")
                junk_score += 2
        
        # Check for suspicious sender patterns
        suspicious_sender_patterns = [
            r'noreply@.*\.tk$',  # Suspicious TLDs
            r'.*@.*\.ml$',
            r'.*@.*\.ga$',
            r'admin@.*',
            r'support@.*',
            r'security@.*',
        ]
        
        for pattern in suspicious_sender_patterns:
            if re.search(pattern, from_addr):
                junk_indicators.append(f"Suspicious sender pattern: {pattern}")
                junk_score += 1
        
        # Check body content for spam indicators
        spam_body_patterns = [
            r'click.*here.*now',
            r'urgent.*respond',
            r'verify.*account.*immediately',
            r'suspended.*account',
            r'winner.*lottery',
            r'inheritance.*million',
            r'bitcoin.*investment',
            r'crypto.*opportunity',
        ]
        
        for pattern in spam_body_patterns:
            if re.search(pattern, body):
                junk_indicators.append(f"Suspicious body content: {pattern}")
                junk_score += 2
        
        # Check for excessive caps
        if len(subject) > 10:
            caps_ratio = sum(1 for c in subject if c.isupper()) / len(subject)
            if caps_ratio > 0.5:
                junk_indicators.append("Excessive capital letters in subject")
                junk_score += 1
        
        # Check for excessive exclamation marks
        exclamation_count = subject.count('!') + body[:500].count('!')
        if exclamation_count > 3:
            junk_indicators.append(f"Excessive exclamation marks ({exclamation_count})")
            junk_score += 1
        
        # Determine junk likelihood
        if junk_score >= 4:
            likelihood = "high"
        elif junk_score >= 2:
            likelihood = "medium"
        elif junk_score >= 1:
            likelihood = "low"
        else:
            likelihood = "unlikely"
        
        return {
            "is_likely_junk": junk_score >= 2,
            "junk_score": junk_score,
            "likelihood": likelihood,
            "indicators": junk_indicators,
            "email_id": email_data.get('id')
        }
    
    def move_email_to_folder(self, email_id: str, target_folder: str, source_folder: str = "INBOX"):
        """Move an email from one folder to another"""
        mail = self.connect_imap()
        mailbox_selected = False
        try:
            status, response = mail.select(source_folder)
            if status != 'OK':
                logger.error(f"Failed to select source folder '{source_folder}': {response}")
                return False
            mailbox_selected = True
            
            # Copy email to target folder
            result = mail.copy(email_id, target_folder)
            if result[0] == 'OK':
                # Mark as deleted in source folder
                mail.store(email_id, '+FLAGS', '\\Deleted')
                mail.expunge()
                return True
            else:
                logger.error(f"Failed to copy email to {target_folder}: {result}")
                return False
        except Exception as e:
            logger.error(f"Failed to move email: {e}")
            return False
        finally:
            if mailbox_selected:
                mail.close()
            mail.logout()
    
    def bulk_move_emails(self, email_ids: List[str], target_folder: str, source_folder: str = "INBOX", batch_size: int = 100):
        """
        Move multiple emails to a folder efficiently using batch operations.
        
        Args:
            email_ids: List of email IDs to move
            target_folder: Destination folder
            source_folder: Source folder (default: INBOX)
            batch_size: Number of emails to process per batch
        
        Returns:
            Dictionary with success/failure counts and details
        """
        if not email_ids:
            return {'moved': 0, 'failed': 0, 'details': []}
        
        mail = self.connect_imap()
        moved_count = 0
        failed_count = 0
        details = []
        mailbox_selected = False
        
        try:
            status, response = mail.select(source_folder)
            if status != 'OK':
                logger.error(f"Failed to select source folder '{source_folder}': {response}")
                return {'moved': 0, 'failed': len(email_ids), 'error': f'Failed to select source folder: {source_folder}'}
            mailbox_selected = True
            
            # Process emails in batches for efficiency
            for i in range(0, len(email_ids), batch_size):
                batch_ids = email_ids[i:i + batch_size]
                id_list = ','.join(batch_ids)
                
                try:
                    # Bulk copy operation
                    result = mail.copy(id_list, target_folder)
                    
                    if result[0] == 'OK':
                        # Bulk mark as deleted
                        mail.store(id_list, '+FLAGS', '\\Deleted')
                        moved_count += len(batch_ids)
                        details.append(f"Successfully moved batch {i//batch_size + 1}: {len(batch_ids)} emails")
                    else:
                        # If batch fails, try individual moves
                        for email_id in batch_ids:
                            try:
                                individual_result = mail.copy(email_id, target_folder)
                                if individual_result[0] == 'OK':
                                    mail.store(email_id, '+FLAGS', '\\Deleted')
                                    moved_count += 1
                                else:
                                    failed_count += 1
                                    details.append(f"Failed to move email {email_id}")
                            except Exception as e:
                                failed_count += 1
                                details.append(f"Error moving email {email_id}: {str(e)}")
                
                except Exception as e:
                    logger.warning(f"Failed to move batch {i//batch_size + 1}: {e}")
                    # Fallback to individual moves for this batch
                    for email_id in batch_ids:
                        try:
                            result = mail.copy(email_id, target_folder)
                            if result[0] == 'OK':
                                mail.store(email_id, '+FLAGS', '\\Deleted')
                                moved_count += 1
                            else:
                                failed_count += 1
                        except Exception as inner_e:
                            failed_count += 1
                            details.append(f"Error moving email {email_id}: {str(inner_e)}")
            
            # Expunge all deleted emails at once
            if moved_count > 0:
                mail.expunge()
                
        except Exception as e:
            logger.error(f"Bulk move operation failed: {e}")
            return {'moved': 0, 'failed': len(email_ids), 'error': str(e)}
        finally:
            if mailbox_selected:
                mail.close()
            mail.logout()
        
        return {
            'moved': moved_count,
            'failed': failed_count,
            'total': len(email_ids),
            'details': details[:10]  # Limit details to first 10 for readability
        }
    
    def bulk_mark_emails(self, email_ids: List[str], flag: str, action: str = "add", mailbox: str = "INBOX", batch_size: int = 100):
        """
        Bulk mark emails with IMAP flags (read, important, etc.).
        
        Args:
            email_ids: List of email IDs to mark
            flag: IMAP flag to set (e.g., '\\Seen', '\\Flagged', '\\Deleted')
            action: 'add' to set flag, 'remove' to unset flag
            mailbox: Mailbox containing the emails
            batch_size: Number of emails to process per batch
        
        Returns:
            Dictionary with success/failure counts
        """
        if not email_ids:
            return {'marked': 0, 'failed': 0}
        
        mail = self.connect_imap()
        marked_count = 0
        failed_count = 0
        mailbox_selected = False
        
        try:
            status, response = mail.select(mailbox)
            if status != 'OK':
                logger.error(f"Failed to select mailbox '{mailbox}': {response}")
                return {'marked': 0, 'failed': len(email_ids), 'error': f'Failed to select mailbox: {mailbox}'}
            mailbox_selected = True
            
            flag_action = '+FLAGS' if action == 'add' else '-FLAGS'
            
            # Process in batches
            for i in range(0, len(email_ids), batch_size):
                batch_ids = email_ids[i:i + batch_size]
                id_list = ','.join(batch_ids)
                
                try:
                    result = mail.store(id_list, flag_action, flag)
                    if result[0] == 'OK':
                        marked_count += len(batch_ids)
                    else:
                        # Fallback to individual marking
                        for email_id in batch_ids:
                            try:
                                individual_result = mail.store(email_id, flag_action, flag)
                                if individual_result[0] == 'OK':
                                    marked_count += 1
                                else:
                                    failed_count += 1
                            except:
                                failed_count += 1
                
                except Exception as e:
                    logger.warning(f"Failed to mark batch {i//batch_size + 1}: {e}")
                    # Fallback to individual marking
                    for email_id in batch_ids:
                        try:
                            result = mail.store(email_id, flag_action, flag)
                            if result[0] == 'OK':
                                marked_count += 1
                            else:
                                failed_count += 1
                        except:
                            failed_count += 1
        
        except Exception as e:
            logger.error(f"Bulk mark operation failed: {e}")
            return {'marked': 0, 'failed': len(email_ids), 'error': str(e)}
        finally:
            if mailbox_selected:
                mail.close()
            mail.logout()
        
        return {'marked': marked_count, 'failed': failed_count, 'total': len(email_ids)}
    
    def bulk_delete_emails(self, email_ids: List[str], mailbox: str = "INBOX", permanent: bool = False):
        """
        Bulk delete emails (move to Trash or permanent deletion).
        
        Args:
            email_ids: List of email IDs to delete
            mailbox: Source mailbox
            permanent: If True, permanently delete; if False, move to Trash
        
        Returns:
            Dictionary with deletion results
        """
        if permanent:
            # Permanent deletion using bulk marking and expunge
            result = self.bulk_mark_emails(email_ids, '\\Deleted', 'add', mailbox)
            if result['marked'] > 0:
                # Expunge to permanently delete
                mail = self.connect_imap()
                mailbox_selected = False
                try:
                    status, response = mail.select(mailbox)
                    if status == 'OK':
                        mailbox_selected = True
                        mail.expunge()
                    else:
                        logger.error(f"Failed to select mailbox '{mailbox}' for expunge: {response}")
                finally:
                    if mailbox_selected:
                        mail.close()
                    mail.logout()
            return {'deleted': result['marked'], 'failed': result['failed'], 'permanent': True}
        else:
            # Move to Trash folder
            return self.bulk_move_emails(email_ids, 'Trash', mailbox)
    
    def get_mailbox_list(self):
        """Get list of available mailboxes/folders"""
        mail = self.connect_imap()
        try:
            status, mailboxes = mail.list()
            if status == 'OK':
                folder_list = []
                for mailbox in mailboxes:
                    # Parse mailbox name from IMAP response
                    parts = mailbox.decode().split('"')
                    if len(parts) >= 3:
                        folder_name = parts[-2]
                        folder_list.append(folder_name)
                return folder_list
            return []
        except Exception as e:
            logger.error(f"Failed to get mailbox list: {e}")
            return []
        finally:
            mail.logout()
    
    def create_folder(self, folder_name: str):
        """Create a new folder/mailbox"""
        mail = self.connect_imap()
        try:
            status, response = mail.create(folder_name)
            if status == 'OK':
                logger.info(f"Successfully created folder: {folder_name}")
                return True
            else:
                logger.error(f"Failed to create folder {folder_name}: {response}")
                return False
        except Exception as e:
            logger.error(f"Failed to create folder: {e}")
            return False
        finally:
            mail.logout()
    
    def delete_folder(self, folder_name: str):
        """Delete an existing folder/mailbox"""
        mail = self.connect_imap()
        try:
            status, response = mail.delete(folder_name)
            if status == 'OK':
                logger.info(f"Successfully deleted folder: {folder_name}")
                return True
            else:
                logger.error(f"Failed to delete folder {folder_name}: {response}")
                return False
        except Exception as e:
            logger.error(f"Failed to delete folder: {e}")
            return False
        finally:
            mail.logout()
    
    def load_filter_rules(self) -> List[Dict]:
        """Load filtering rules from JSON file"""
        try:
            if os.path.exists(self.rules_file):
                with open(self.rules_file, 'r') as f:
                    return json.load(f)
            return []
        except Exception as e:
            logger.error(f"Failed to load filter rules: {e}")
            return []
    
    def save_filter_rules(self, rules: List[Dict]) -> bool:
        """Save filtering rules to JSON file"""
        try:
            with open(self.rules_file, 'w') as f:
                json.dump(rules, f, indent=2)
            return True
        except Exception as e:
            logger.error(f"Failed to save filter rules: {e}")
            return False
    
    def create_filter_rule(self, rule_name: str, conditions: Dict, actions: Dict, enabled: bool = True) -> bool:
        """
        Create a new email filtering rule.
        
        Args:
            rule_name: Unique name for the rule
            conditions: Dictionary of conditions to match (e.g., {'from': 'sender@example.com', 'subject_contains': 'newsletter'})
            actions: Dictionary of actions to take (e.g., {'move_to_folder': 'Newsletter', 'mark_as_read': True})
            enabled: Whether the rule is active
        
        Returns:
            True if rule was created successfully
        """
        rules = self.load_filter_rules()
        
        # Check if rule name already exists
        if any(rule['name'] == rule_name for rule in rules):
            logger.error(f"Rule with name '{rule_name}' already exists")
            return False
        
        # Validate conditions
        valid_conditions = [
            'from', 'to', 'subject_contains', 'subject_equals', 'body_contains',
            'sender_domain', 'has_attachments', 'older_than_days', 'newer_than_days'
        ]
        for condition in conditions.keys():
            if condition not in valid_conditions:
                logger.error(f"Invalid condition: {condition}")
                return False
        
        # Validate actions
        valid_actions = [
            'move_to_folder', 'mark_as_read', 'mark_as_important', 'delete',
            'forward_to', 'auto_reply'
        ]
        for action in actions.keys():
            if action not in valid_actions:
                logger.error(f"Invalid action: {action}")
                return False
        
        # Create new rule
        new_rule = {
            'id': str(len(rules) + 1),
            'name': rule_name,
            'conditions': conditions,
            'actions': actions,
            'enabled': enabled,
            'created_at': datetime.now().isoformat(),
            'last_applied': None,
            'emails_processed': 0
        }
        
        rules.append(new_rule)
        return self.save_filter_rules(rules)
    
    def delete_filter_rule(self, rule_id: str) -> bool:
        """Delete a filtering rule by ID"""
        rules = self.load_filter_rules()
        updated_rules = [rule for rule in rules if rule['id'] != rule_id]
        
        if len(updated_rules) == len(rules):
            logger.error(f"Rule with ID '{rule_id}' not found")
            return False
        
        return self.save_filter_rules(updated_rules)
    
    def update_filter_rule(self, rule_id: str, **updates) -> bool:
        """Update an existing filtering rule"""
        rules = self.load_filter_rules()
        
        for rule in rules:
            if rule['id'] == rule_id:
                rule.update(updates)
                return self.save_filter_rules(rules)
        
        logger.error(f"Rule with ID '{rule_id}' not found")
        return False
    
    def email_matches_rule(self, email_data: Dict, rule: Dict) -> bool:
        """Check if an email matches a filtering rule's conditions"""
        conditions = rule.get('conditions', {})
        
        for condition, value in conditions.items():
            if condition == 'from':
                if value.lower() not in email_data.get('from', '').lower():
                    return False
            elif condition == 'to':
                if value.lower() not in email_data.get('to', '').lower():
                    return False
            elif condition == 'subject_contains':
                if value.lower() not in email_data.get('subject', '').lower():
                    return False
            elif condition == 'subject_equals':
                if value.lower() != email_data.get('subject', '').lower():
                    return False
            elif condition == 'body_contains':
                if value.lower() not in email_data.get('body', '').lower():
                    return False
            elif condition == 'sender_domain':
                sender = email_data.get('from', '')
                domain = sender.split('@')[-1] if '@' in sender else ''
                if value.lower() != domain.lower():
                    return False
            elif condition == 'has_attachments':
                # This would need to be implemented based on email structure
                pass
            elif condition == 'older_than_days' or condition == 'newer_than_days':
                # This would need date parsing and comparison
                pass
        
        return True
    
    def apply_rule_actions(self, email_id: str, rule: Dict, mailbox: str = "INBOX") -> Dict[str, Any]:
        """Apply the actions specified in a filtering rule to an email"""
        actions = rule.get('actions', {})
        results = {}
        
        for action, value in actions.items():
            try:
                if action == 'move_to_folder':
                    success = self.move_email_to_folder(email_id, value, mailbox)
                    results[action] = {'success': success, 'target_folder': value}
                elif action == 'mark_as_read':
                    if value:  # Only if True
                        # Implementation would require IMAP STORE command
                        results[action] = {'success': True, 'marked_read': True}
                elif action == 'delete':
                    if value:  # Only if True
                        success = self.move_email_to_folder(email_id, 'Trash', mailbox)
                        results[action] = {'success': success}
                elif action == 'mark_as_important':
                    if value:  # Only if True
                        # Implementation would require IMAP flag setting
                        results[action] = {'success': True, 'marked_important': True}
                # Forward and auto-reply would be more complex implementations
            except Exception as e:
                results[action] = {'success': False, 'error': str(e)}
        
        return results
    
    def apply_filter_rules(self, mailbox: str = "INBOX", limit: int = 50) -> Dict[str, Any]:
        """Apply all enabled filtering rules to emails in a mailbox using bulk operations"""
        rules = self.load_filter_rules()
        enabled_rules = [rule for rule in rules if rule.get('enabled', True)]
        
        if not enabled_rules:
            return {'message': 'No enabled rules found', 'emails_processed': 0, 'actions_taken': 0}
        
        # Get recent emails
        emails = self.search_emails("ALL", mailbox, limit)
        if not emails or (len(emails) == 1 and "error" in emails[0]):
            return {'error': 'Failed to retrieve emails'}
        
        # Extract email IDs for bulk retrieval
        email_ids = [email['id'] for email in emails]
        
        # Bulk retrieve full email content
        logger.info(f"Bulk retrieving {len(email_ids)} emails for rule processing")
        full_emails = self.get_bulk_emails(email_ids, mailbox)
        
        if not full_emails:
            return {'error': 'Failed to retrieve email content'}
        
        # Group actions by type for bulk execution
        bulk_actions = {
            'move_to_folder': {},  # {target_folder: [email_ids]}
            'mark_as_read': [],
            'mark_as_important': [],
            'delete': []
        }
        
        processed_count = 0
        rule_results = []
        
        # Process each email against all rules
        for email_id, full_email in full_emails.items():
            processed_count += 1
            
            for rule in enabled_rules:
                if self.email_matches_rule(full_email, rule):
                    actions = rule.get('actions', {})
                    
                    # Queue actions for bulk execution instead of executing individually
                    for action, value in actions.items():
                        if action == 'move_to_folder' and value:
                            if value not in bulk_actions['move_to_folder']:
                                bulk_actions['move_to_folder'][value] = []
                            bulk_actions['move_to_folder'][value].append(email_id)
                        elif action == 'mark_as_read' and value:
                            bulk_actions['mark_as_read'].append(email_id)
                        elif action == 'mark_as_important' and value:
                            bulk_actions['mark_as_important'].append(email_id)
                        elif action == 'delete' and value:
                            bulk_actions['delete'].append(email_id)
                    
                    rule_result = {
                        'rule_id': rule['id'],
                        'rule_name': rule['name'],
                        'email_id': email_id,
                        'email_subject': full_email.get('subject', ''),
                        'actions_queued': list(actions.keys())
                    }
                    rule_results.append(rule_result)
                    
                    # Update rule statistics
                    rule['emails_processed'] = rule.get('emails_processed', 0) + 1
                    rule['last_applied'] = datetime.now().isoformat()
        
        # Execute bulk actions
        actions_taken = 0
        bulk_results = {}
        
        # Bulk move operations
        for target_folder, email_ids_to_move in bulk_actions['move_to_folder'].items():
            if email_ids_to_move:
                logger.info(f"Bulk moving {len(email_ids_to_move)} emails to {target_folder}")
                move_result = self.bulk_move_emails(email_ids_to_move, target_folder, mailbox)
                bulk_results[f'move_to_{target_folder}'] = move_result
                actions_taken += move_result.get('moved', 0)
        
        # Bulk mark as read
        if bulk_actions['mark_as_read']:
            logger.info(f"Bulk marking {len(bulk_actions['mark_as_read'])} emails as read")
            read_result = self.bulk_mark_emails(bulk_actions['mark_as_read'], '\\Seen', 'add', mailbox)
            bulk_results['mark_as_read'] = read_result
            actions_taken += read_result.get('marked', 0)
        
        # Bulk mark as important
        if bulk_actions['mark_as_important']:
            logger.info(f"Bulk marking {len(bulk_actions['mark_as_important'])} emails as important")
            important_result = self.bulk_mark_emails(bulk_actions['mark_as_important'], '\\Flagged', 'add', mailbox)
            bulk_results['mark_as_important'] = important_result
            actions_taken += important_result.get('marked', 0)
        
        # Bulk delete
        if bulk_actions['delete']:
            logger.info(f"Bulk deleting {len(bulk_actions['delete'])} emails")
            delete_result = self.bulk_delete_emails(bulk_actions['delete'], mailbox, permanent=False)
            bulk_results['delete'] = delete_result
            actions_taken += delete_result.get('deleted', 0)
        
        # Save updated rule statistics
        self.save_filter_rules(rules)
        
        return {
            'emails_processed': processed_count,
            'rules_applied': len(rule_results),
            'actions_taken': actions_taken,
            'bulk_results': bulk_results,
            'rule_results': rule_results[:20]  # Limit detailed results for readability
        }
    
    def apply_filter_rules_optimized(self, mailbox: str = "INBOX", limit: int = 200, chunk_size: int = 50) -> Dict[str, Any]:
        """
        Highly optimized filter rule application for large email volumes.
        Processes emails in chunks to manage memory usage.
        """
        rules = self.load_filter_rules()
        enabled_rules = [rule for rule in rules if rule.get('enabled', True)]
        
        if not enabled_rules:
            return {'message': 'No enabled rules found', 'emails_processed': 0, 'actions_taken': 0}
        
        # Get all emails for processing
        emails = self.search_emails("ALL", mailbox, limit)
        if not emails or (len(emails) == 1 and "error" in emails[0]):
            return {'error': 'Failed to retrieve emails'}
        
        email_ids = [email['id'] for email in emails]
        total_processed = 0
        total_actions = 0
        all_bulk_results = {}
        
        # Process emails in chunks to manage memory
        for i in range(0, len(email_ids), chunk_size):
            chunk_ids = email_ids[i:i + chunk_size]
            logger.info(f"Processing chunk {i//chunk_size + 1}/{(len(email_ids) + chunk_size - 1)//chunk_size}: {len(chunk_ids)} emails")
            
            # Apply rules to this chunk
            chunk_result = self.apply_filter_rules_to_chunk(chunk_ids, enabled_rules, mailbox)
            
            total_processed += chunk_result.get('emails_processed', 0)
            total_actions += chunk_result.get('actions_taken', 0)
            
            # Merge bulk results
            for action_type, result in chunk_result.get('bulk_results', {}).items():
                if action_type not in all_bulk_results:
                    all_bulk_results[action_type] = {'moved': 0, 'marked': 0, 'deleted': 0, 'failed': 0}
                
                for key in ['moved', 'marked', 'deleted', 'failed']:
                    if key in result:
                        all_bulk_results[action_type][key] = all_bulk_results[action_type].get(key, 0) + result[key]
        
        # Save updated rule statistics
        self.save_filter_rules(rules)
        
        return {
            'emails_processed': total_processed,
            'actions_taken': total_actions,
            'bulk_results': all_bulk_results,
            'chunks_processed': (len(email_ids) + chunk_size - 1) // chunk_size
        }
    
    def apply_filter_rules_to_chunk(self, email_ids: List[str], rules: List[Dict], mailbox: str) -> Dict[str, Any]:
        """Apply filter rules to a chunk of emails"""
        # Bulk retrieve emails for this chunk
        full_emails = self.get_bulk_emails(email_ids, mailbox)
        
        if not full_emails:
            return {'emails_processed': 0, 'actions_taken': 0, 'bulk_results': {}}
        
        # Group actions for bulk execution
        bulk_actions = {
            'move_to_folder': {},
            'mark_as_read': [],
            'mark_as_important': [],
            'delete': []
        }
        
        # Process emails against rules
        for email_id, full_email in full_emails.items():
            for rule in rules:
                if self.email_matches_rule(full_email, rule):
                    actions = rule.get('actions', {})
                    
                    for action, value in actions.items():
                        if action == 'move_to_folder' and value:
                            if value not in bulk_actions['move_to_folder']:
                                bulk_actions['move_to_folder'][value] = []
                            bulk_actions['move_to_folder'][value].append(email_id)
                        elif action == 'mark_as_read' and value:
                            bulk_actions['mark_as_read'].append(email_id)
                        elif action == 'mark_as_important' and value:
                            bulk_actions['mark_as_important'].append(email_id)
                        elif action == 'delete' and value:
                            bulk_actions['delete'].append(email_id)
        
        # Execute bulk actions for this chunk
        actions_taken = 0
        bulk_results = {}
        
        for target_folder, move_ids in bulk_actions['move_to_folder'].items():
            if move_ids:
                result = self.bulk_move_emails(move_ids, target_folder, mailbox)
                bulk_results[f'move_to_{target_folder}'] = result
                actions_taken += result.get('moved', 0)
        
        if bulk_actions['mark_as_read']:
            result = self.bulk_mark_emails(bulk_actions['mark_as_read'], '\\Seen', 'add', mailbox)
            bulk_results['mark_as_read'] = result
            actions_taken += result.get('marked', 0)
        
        if bulk_actions['mark_as_important']:
            result = self.bulk_mark_emails(bulk_actions['mark_as_important'], '\\Flagged', 'add', mailbox)
            bulk_results['mark_as_important'] = result
            actions_taken += result.get('marked', 0)
        
        if bulk_actions['delete']:
            result = self.bulk_delete_emails(bulk_actions['delete'], mailbox)
            bulk_results['delete'] = result
            actions_taken += result.get('deleted', 0)
        
        return {
            'emails_processed': len(full_emails),
            'actions_taken': actions_taken,
            'bulk_results': bulk_results
        }
    
    def get_full_email_with_html(self, email_id: str, mailbox: str = "INBOX"):
        """Get full email content including HTML parts for link extraction"""
        mail = self.connect_imap()
        mailbox_selected = False
        try:
            status, response = mail.select(mailbox)
            if status != 'OK':
                logger.error(f"Failed to select mailbox '{mailbox}': {response}")
                return None
            mailbox_selected = True
            
            status, msg_data = mail.fetch(email_id, '(RFC822)')
            
            if status != 'OK':
                return None
            
            raw_email = msg_data[0][1]
            email_message = email.message_from_bytes(raw_email)
            
            # Extract both text and HTML content
            text_content = ""
            html_content = ""
            
            if email_message.is_multipart():
                for part in email_message.walk():
                    content_type = part.get_content_type()
                    content_disposition = str(part.get("Content-Disposition"))
                    
                    if "attachment" not in content_disposition:
                        try:
                            payload = part.get_payload(decode=True)
                            if payload:
                                content = payload.decode('utf-8', errors='ignore')
                                if content_type == "text/plain":
                                    text_content += content
                                elif content_type == "text/html":
                                    html_content += content
                        except:
                            continue
            else:
                try:
                    payload = email_message.get_payload(decode=True)
                    if payload:
                        content = payload.decode('utf-8', errors='ignore')
                        if email_message.get_content_type() == "text/html":
                            html_content = content
                        else:
                            text_content = content
                except:
                    pass
            
            return {
                'id': email_id,
                'subject': self.decode_mime_words(email_message['Subject']),
                'from': self.decode_mime_words(email_message['From']),
                'to': self.decode_mime_words(email_message['To']),
                'date': email_message['Date'],
                'text_body': text_content,
                'html_body': html_content,
                'list_unsubscribe': email_message.get('List-Unsubscribe', ''),
                'list_unsubscribe_post': email_message.get('List-Unsubscribe-Post', '')
            }
        finally:
            if mailbox_selected:
                mail.close()
            mail.logout()
    
    def find_unsubscribe_links(self, email_data: Dict) -> Dict[str, Any]:
        """
        Find unsubscribe links and methods in an email.
        Returns various unsubscribe options found.
        """
        unsubscribe_methods = []
        
        # Check List-Unsubscribe header (RFC 2369)
        list_unsubscribe = email_data.get('list_unsubscribe', '')
        if list_unsubscribe:
            # Parse mailto and HTTP URLs from List-Unsubscribe header
            mailto_matches = re.findall(r'<mailto:([^>]+)>', list_unsubscribe)
            http_matches = re.findall(r'<(https?://[^>]+)>', list_unsubscribe)
            
            for mailto in mailto_matches:
                unsubscribe_methods.append({
                    'type': 'mailto',
                    'address': mailto,
                    'method': 'email'
                })
            
            for url in http_matches:
                unsubscribe_methods.append({
                    'type': 'http',
                    'url': url,
                    'method': 'click'
                })
        
        # Check for one-click unsubscribe (RFC 8058)
        list_unsubscribe_post = email_data.get('list_unsubscribe_post', '')
        if list_unsubscribe_post and 'List-Unsubscribe=One-Click' in list_unsubscribe_post:
            # Find the corresponding HTTP URL for one-click
            for method in unsubscribe_methods:
                if method['type'] == 'http':
                    method['one_click'] = True
                    method['method'] = 'one_click'
        
        # Extract unsubscribe links from email body
        text_body = email_data.get('text_body', '')
        html_body = email_data.get('html_body', '')
        
        # Common unsubscribe link patterns
        unsubscribe_patterns = [
            r'(?i)unsubscribe.*?https?://[^\s<>"]+',
            r'(?i)https?://[^\s<>"]*unsubscribe[^\s<>"]*',
            r'(?i)https?://[^\s<>"]*opt.?out[^\s<>"]*',
            r'(?i)https?://[^\s<>"]*remove[^\s<>"]*',
        ]
        
        # Search in HTML content for href attributes
        if html_body:
            html_unsubscribe_patterns = [
                r'(?i)<a[^>]*href=["\']([^"\']*unsubscribe[^"\']*)["\'][^>]*>',
                r'(?i)<a[^>]*href=["\']([^"\']*opt.?out[^"\']*)["\'][^>]*>',
                r'(?i)<a[^>]*href=["\']([^"\']*remove[^"\']*)["\'][^>]*>',
            ]
            
            for pattern in html_unsubscribe_patterns:
                matches = re.findall(pattern, html_body)
                for match in matches:
                    if match.startswith('http'):
                        unsubscribe_methods.append({
                            'type': 'http',
                            'url': match,
                            'method': 'click',
                            'source': 'html_body'
                        })
        
        # Search in text content
        for pattern in unsubscribe_patterns:
            matches = re.findall(pattern, text_body)
            for match in matches:
                # Extract just the URL part
                url_match = re.search(r'https?://[^\s<>"]+', match)
                if url_match:
                    unsubscribe_methods.append({
                        'type': 'http',
                        'url': url_match.group(),
                        'method': 'click',
                        'source': 'text_body'
                    })
        
        # Remove duplicates
        seen_urls = set()
        unique_methods = []
        for method in unsubscribe_methods:
            identifier = method.get('url') or method.get('address')
            if identifier not in seen_urls:
                seen_urls.add(identifier)
                unique_methods.append(method)
        
        return {
            'email_id': email_data.get('id'),
            'subject': email_data.get('subject'),
            'from': email_data.get('from'),
            'unsubscribe_methods': unique_methods,
            'total_methods': len(unique_methods),
            'has_one_click': any(method.get('one_click', False) for method in unique_methods)
        }
    
    def execute_unsubscribe(self, unsubscribe_method: Dict, timeout: int = 10) -> Dict[str, Any]:
        """
        Execute an unsubscribe request.
        """
        result = {
            'method': unsubscribe_method,
            'success': False,
            'message': '',
            'status_code': None
        }
        
        try:
            if unsubscribe_method['type'] == 'mailto':
                # For mailto unsubscribe, we would send an email
                # This is more complex and requires careful handling
                result['message'] = "Mailto unsubscribe requires manual email sending"
                result['success'] = False
                
            elif unsubscribe_method['type'] == 'http':
                url = unsubscribe_method['url']
                
                # Set up headers to look like a real browser
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.5',
                    'Accept-Encoding': 'gzip, deflate',
                    'Connection': 'keep-alive',
                }
                
                if unsubscribe_method.get('one_click'):
                    # RFC 8058 one-click unsubscribe
                    headers['List-Unsubscribe'] = 'One-Click'
                    response = requests.post(url, headers=headers, timeout=timeout, allow_redirects=True)
                else:
                    # Regular HTTP GET request
                    response = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
                
                result['status_code'] = response.status_code
                
                if response.status_code in [200, 201, 202, 204]:
                    result['success'] = True
                    result['message'] = f"Unsubscribe request successful (HTTP {response.status_code})"
                else:
                    result['message'] = f"Unsubscribe request failed with HTTP {response.status_code}"
                
                # Check response content for confirmation
                if result['success']:
                    content = response.text.lower()
                    confirmation_phrases = [
                        'unsubscribed', 'removed', 'opted out', 'no longer receive',
                        'successfully unsubscribed', 'email address has been removed'
                    ]
                    if any(phrase in content for phrase in confirmation_phrases):
                        result['message'] += " - Confirmation detected in response"
                
        except requests.exceptions.Timeout:
            result['message'] = f"Request timed out after {timeout} seconds"
        except requests.exceptions.ConnectionError:
            result['message'] = "Connection error - could not reach unsubscribe URL"
        except requests.exceptions.RequestException as e:
            result['message'] = f"Request failed: {str(e)}"
        except Exception as e:
            result['message'] = f"Unexpected error: {str(e)}"
        
        return result

# Initialize email client
email_client = ProtonEmailClient()

# MCP Tools
@mcp.tool()
def search_emails(query: str = "ALL", mailbox: str = "INBOX", limit: int = 10) -> List[dict]:
    """
    Search for emails in your Proton mailbox.
    
    Args:
        query: IMAP search query (e.g., 'FROM "sender@example.com"', 'SUBJECT "important"')
        mailbox: Mailbox to search in (default: INBOX)
        limit: Maximum number of emails to return (default: 10)
    
    Returns:
        List of email summaries with id, subject, from, date, and body preview
    """
    try:
        return email_client.search_emails(query, mailbox, limit)
    except Exception as e:
        return [{"error": f"Failed to search emails: {str(e)}"}]

@mcp.tool()
def get_email_content(email_id: str, mailbox: str = "INBOX") -> dict:
    """
    Get the full content of a specific email.
    
    Args:
        email_id: The ID of the email to retrieve
        mailbox: Mailbox containing the email (default: INBOX)
    
    Returns:
        Full email content including subject, from, to, date, and complete body
    """
    try:
        email_data = email_client.get_full_email(email_id, mailbox)
        if email_data:
            return email_data
        else:
            return {"error": "Email not found"}
    except Exception as e:
        return {"error": f"Failed to get email: {str(e)}"}

@mcp.tool()
def send_email(to: str, subject: str, body: str, reply_to_id: str = None) -> dict:
    """
    Send an email via Proton Mail.
    
    Args:
        to: Recipient email address
        subject: Email subject
        body: Email body content
        reply_to_id: Optional ID of email being replied to
    
    Returns:
        Success status and message
    """
    try:
        success = email_client.send_email(to, subject, body, reply_to_id)
        if success:
            return {"status": "success", "message": "Email sent successfully"}
        else:
            return {"status": "error", "message": "Failed to send email"}
    except Exception as e:
        return {"status": "error", "message": f"Failed to send email: {str(e)}"}

@mcp.tool()
def get_recent_emails(days: int = 7, mailbox: str = "INBOX") -> List[dict]:
    """
    Get recent emails from the specified number of days.
    
    Args:
        days: Number of days back to search (default: 7)
        mailbox: Mailbox to search in (default: INBOX)
    
    Returns:
        List of recent emails
    """
    try:
        since_date = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        query = f'SINCE "{since_date}"'
        return email_client.search_emails(query, mailbox, 20)
    except Exception as e:
        return [{"error": f"Failed to get recent emails: {str(e)}"}]

@mcp.tool()
def filter_junk_emails(mailbox: str = "INBOX", limit: int = 20, action: str = "analyze") -> List[dict]:
    """
    Filter and analyze emails for junk/spam content using optimized bulk operations.
    
    Args:
        mailbox: Mailbox to analyze (default: INBOX)
        limit: Maximum number of emails to analyze (default: 20)
        action: Action to take - "analyze" (default) or "move_to_spam"
    
    Returns:
        List of emails with junk analysis results
    """
    try:
        # Get recent emails
        emails = email_client.search_emails("ALL", mailbox, limit)
        if not emails or (len(emails) == 1 and "error" in emails[0]):
            return emails
        
        # Extract email IDs for bulk retrieval
        email_ids = [email['id'] for email in emails]
        
        # Bulk retrieve full email content
        logger.info(f"Bulk analyzing {len(email_ids)} emails for junk content")
        full_emails = email_client.get_bulk_emails(email_ids, mailbox)
        
        if not full_emails:
            return [{"error": "Failed to retrieve email content for junk analysis"}]
        
        filtered_results = []
        junk_email_ids = []
        
        # Analyze each email for junk
        for email_item in emails:
            email_id = email_item['id']
            full_email = full_emails.get(email_id)
            
            if not full_email:
                continue
                
            # Analyze for junk
            junk_analysis = email_client.is_junk_email(full_email)
            
            # Add original email data to result
            result = {
                **email_item,
                "junk_analysis": junk_analysis
            }
            
            # Queue junk emails for bulk move
            if action == "move_to_spam" and junk_analysis["is_likely_junk"]:
                junk_email_ids.append(email_id)
                result["action_queued"] = "move_to_spam"
            
            filtered_results.append(result)
        
        # Bulk move junk emails if requested
        moved_count = 0
        if action == "move_to_spam" and junk_email_ids:
            logger.info(f"Bulk moving {len(junk_email_ids)} junk emails to Spam folder")
            move_result = email_client.bulk_move_emails(junk_email_ids, "Spam", mailbox)
            moved_count = move_result.get('moved', 0)
            
            # Update results with actual move status
            for result in filtered_results:
                if result.get("action_queued") == "move_to_spam":
                    email_id = result['id']
                    if email_id in junk_email_ids[:moved_count]:  # Successful moves
                        result["action_taken"] = "moved_to_spam"
                    else:
                        result["action_taken"] = "move_failed"
                    result.pop("action_queued", None)
        
        # Add summary
        junk_count = sum(1 for r in filtered_results if r["junk_analysis"]["is_likely_junk"])
        summary = {
            "summary": {
                "total_analyzed": len(filtered_results),
                "junk_detected": junk_count,
                "moved_to_spam": moved_count if action == "move_to_spam" else 0,
                "move_failed": len(junk_email_ids) - moved_count if action == "move_to_spam" else 0
            }
        }
        filtered_results.insert(0, summary)
        
        return filtered_results
    except Exception as e:
        return [{"error": f"Failed to filter junk emails: {str(e)}"}]

@mcp.tool()
def move_email_to_folder(email_id: str, target_folder: str, source_folder: str = "INBOX") -> dict:
    """
    Move a specific email to a different folder.
    
    Args:
        email_id: The ID of the email to move
        target_folder: Target folder name (e.g., "Spam", "Trash", "Archive")
        source_folder: Source folder name (default: INBOX)
    
    Returns:
        Status of the move operation
    """
    try:
        success = email_client.move_email_to_folder(email_id, target_folder, source_folder)
        if success:
            return {
                "status": "success", 
                "message": f"Email {email_id} moved from {source_folder} to {target_folder}"
            }
        else:
            return {
                "status": "error", 
                "message": f"Failed to move email {email_id}"
            }
    except Exception as e:
        return {"status": "error", "message": f"Error moving email: {str(e)}"}

@mcp.tool()
def get_mailboxes() -> List[str]:
    """
    Get list of available mailboxes/folders.
    
    Returns:
        List of mailbox names
    """
    try:
        return email_client.get_mailbox_list()
    except Exception as e:
        return [f"Error: {str(e)}"]

@mcp.tool()
def create_folder(folder_name: str) -> dict:
    """
    Create a new email folder/mailbox.
    
    Args:
        folder_name: Name of the folder to create
    
    Returns:
        Status of the folder creation operation
    """
    try:
        success = email_client.create_folder(folder_name)
        if success:
            return {
                "status": "success",
                "message": f"Folder '{folder_name}' created successfully"
            }
        else:
            return {
                "status": "error",
                "message": f"Failed to create folder '{folder_name}'"
            }
    except Exception as e:
        return {"status": "error", "message": f"Error creating folder: {str(e)}"}

@mcp.tool()
def delete_folder(folder_name: str) -> dict:
    """
    Delete an existing email folder/mailbox.
    
    Args:
        folder_name: Name of the folder to delete
    
    Returns:
        Status of the folder deletion operation
    """
    try:
        success = email_client.delete_folder(folder_name)
        if success:
            return {
                "status": "success",
                "message": f"Folder '{folder_name}' deleted successfully"
            }
        else:
            return {
                "status": "error",
                "message": f"Failed to delete folder '{folder_name}'"
            }
    except Exception as e:
        return {"status": "error", "message": f"Error deleting folder: {str(e)}"}

@mcp.tool()
def analyze_email_for_junk(email_id: str, mailbox: str = "INBOX") -> dict:
    """
    Analyze a specific email for junk/spam indicators.
    
    Args:
        email_id: The ID of the email to analyze
        mailbox: Mailbox containing the email (default: INBOX)
    
    Returns:
        Detailed junk analysis for the email
    """
    try:
        # Get full email content
        email_data = email_client.get_full_email(email_id, mailbox)
        if not email_data:
            return {"error": "Email not found"}
        
        # Analyze for junk
        junk_analysis = email_client.is_junk_email(email_data)
        
        return {
            "email": {
                "id": email_data["id"],
                "subject": email_data["subject"],
                "from": email_data["from"],
                "date": email_data["date"]
            },
            "junk_analysis": junk_analysis
        }
    except Exception as e:
        return {"error": f"Failed to analyze email: {str(e)}"}

@mcp.tool()
def search_emails_filtered(query: str = "ALL", mailbox: str = "INBOX", limit: int = 10, exclude_junk: bool = False) -> List[dict]:
    """
    Search for emails with optional junk filtering.
    
    Args:
        query: IMAP search query (e.g., 'FROM "sender@example.com"', 'SUBJECT "important"')
        mailbox: Mailbox to search in (default: INBOX)  
        limit: Maximum number of emails to return (default: 10)
        exclude_junk: If True, filter out likely junk emails (default: False)
    
    Returns:
        List of email summaries, optionally filtered for junk
    """
    try:
        emails = email_client.search_emails(query, mailbox, limit if not exclude_junk else limit * 2)
        if not emails or (len(emails) == 1 and "error" in emails[0]):
            return emails
        
        if not exclude_junk:
            return emails
        
        # Filter out junk emails
        filtered_emails = []
        for email_item in emails:
            if len(filtered_emails) >= limit:
                break
                
            # Get full email content for junk analysis
            full_email = email_client.get_full_email(email_item['id'], mailbox)
            if full_email:
                junk_analysis = email_client.is_junk_email(full_email)
                if not junk_analysis["is_likely_junk"]:
                    filtered_emails.append(email_item)
        
        return filtered_emails
    except Exception as e:
        return [{"error": f"Failed to search emails: {str(e)}"}]

@mcp.tool()
def find_unsubscribe_links(email_id: str, mailbox: str = "INBOX") -> dict:
    """
    Find unsubscribe links and methods in a specific email.
    
    Args:
        email_id: The ID of the email to analyze
        mailbox: Mailbox containing the email (default: INBOX)
    
    Returns:
        Dictionary with unsubscribe methods found
    """
    try:
        # Get full email content with HTML
        email_data = email_client.get_full_email_with_html(email_id, mailbox)
        if not email_data:
            return {"error": "Email not found"}
        
        # Find unsubscribe methods
        unsubscribe_info = email_client.find_unsubscribe_links(email_data)
        
        return unsubscribe_info
    except Exception as e:
        return {"error": f"Failed to find unsubscribe links: {str(e)}"}

@mcp.tool()
def unsubscribe_from_email(email_id: str, mailbox: str = "INBOX", method_index: int = 0, confirm: bool = False) -> dict:
    """
    Unsubscribe from a mailing list using links found in an email.
    
    Args:
        email_id: The ID of the email containing unsubscribe links
        mailbox: Mailbox containing the email (default: INBOX)
        method_index: Index of unsubscribe method to use (default: 0 for first method)
        confirm: Must be True to actually execute unsubscribe (safety measure)
    
    Returns:
        Result of unsubscribe attempt
    """
    try:
        if not confirm:
            return {
                "error": "Safety measure: set confirm=True to execute unsubscribe",
                "message": "Use find_unsubscribe_links first to see available methods"
            }
        
        # Get full email content with HTML
        email_data = email_client.get_full_email_with_html(email_id, mailbox)
        if not email_data:
            return {"error": "Email not found"}
        
        # Find unsubscribe methods
        unsubscribe_info = email_client.find_unsubscribe_links(email_data)
        
        if not unsubscribe_info['unsubscribe_methods']:
            return {"error": "No unsubscribe methods found in this email"}
        
        if method_index >= len(unsubscribe_info['unsubscribe_methods']):
            return {
                "error": f"Method index {method_index} out of range. Available methods: 0-{len(unsubscribe_info['unsubscribe_methods'])-1}"
            }
        
        # Execute unsubscribe
        method = unsubscribe_info['unsubscribe_methods'][method_index]
        result = email_client.execute_unsubscribe(method)
        
        return {
            "email_info": {
                "id": email_data['id'],
                "subject": email_data['subject'],
                "from": email_data['from']
            },
            "unsubscribe_result": result
        }
    except Exception as e:
        return {"error": f"Failed to unsubscribe: {str(e)}"}

@mcp.tool()
def bulk_find_unsubscribe_opportunities(mailbox: str = "INBOX", days: int = 30, limit: int = 50) -> List[dict]:
    """
    Scan recent emails to find unsubscribe opportunities from mailing lists using bulk operations.
    
    Args:
        mailbox: Mailbox to scan (default: INBOX)
        days: Number of days back to search (default: 30)
        limit: Maximum emails to analyze (default: 50)
    
    Returns:
        List of emails with unsubscribe opportunities
    """
    try:
        # Get recent emails
        since_date = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        query = f'SINCE "{since_date}"'
        emails = email_client.search_emails(query, mailbox, limit)
        
        if not emails or (len(emails) == 1 and "error" in emails[0]):
            return emails
        
        # Extract email IDs for bulk retrieval
        email_ids = [email['id'] for email in emails]
        
        # Bulk retrieve emails with HTML content
        logger.info(f"Bulk analyzing {len(email_ids)} emails for unsubscribe opportunities")
        full_emails = email_client.get_bulk_emails_with_html(email_ids, mailbox)
        
        if not full_emails:
            return [{"error": "Failed to retrieve email content for unsubscribe analysis"}]
        
        unsubscribe_opportunities = []
        processed = 0
        
        for email_item in emails:
            email_id = email_item['id']
            email_data = full_emails.get(email_id)
            
            if not email_data:
                continue
            
            processed += 1
            
            try:
                # Find unsubscribe methods
                unsubscribe_info = email_client.find_unsubscribe_links(email_data)
                
                if unsubscribe_info['total_methods'] > 0:
                    # Add basic email info
                    opportunity = {
                        **email_item,
                        "unsubscribe_info": unsubscribe_info
                    }
                    unsubscribe_opportunities.append(opportunity)
                    
            except Exception as e:
                logger.warning(f"Failed to analyze email {email_id}: {e}")
                continue
        
        # Add summary
        summary = {
            "summary": {
                "emails_analyzed": processed,
                "unsubscribe_opportunities": len(unsubscribe_opportunities),
                "one_click_available": sum(1 for opp in unsubscribe_opportunities 
                                         if opp["unsubscribe_info"]["has_one_click"])
            }
        }
        
        return [summary] + unsubscribe_opportunities
    except Exception as e:
        return [{"error": f"Failed to find unsubscribe opportunities: {str(e)}"}]

@mcp.tool()
def get_mailing_list_senders(mailbox: str = "INBOX", days: int = 30, min_emails: int = 2) -> List[dict]:
    """
    Identify frequent senders that might be mailing lists.
    
    Args:
        mailbox: Mailbox to analyze (default: INBOX)
        days: Number of days back to analyze (default: 30)
        min_emails: Minimum number of emails to consider sender as mailing list (default: 2)
    
    Returns:
        List of potential mailing list senders with email counts
    """
    try:
        # Get recent emails
        since_date = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        query = f'SINCE "{since_date}"'
        emails = email_client.search_emails(query, mailbox, 200)  # Higher limit for analysis
        
        if not emails or (len(emails) == 1 and "error" in emails[0]):
            return emails
        
        # Count emails per sender
        sender_counts = {}
        for email_item in emails:
            from_addr = email_item.get('from', '').strip()
            if from_addr:
                if from_addr not in sender_counts:
                    sender_counts[from_addr] = {
                        'count': 0,
                        'subjects': [],
                        'latest_date': email_item.get('date', '')
                    }
                sender_counts[from_addr]['count'] += 1
                sender_counts[from_addr]['subjects'].append(email_item.get('subject', 'No Subject'))
        
        # Filter for potential mailing lists
        mailing_lists = []
        for sender, data in sender_counts.items():
            if data['count'] >= min_emails:
                # Check for mailing list indicators
                is_likely_mailing_list = any([
                    'newsletter' in sender.lower(),
                    'noreply' in sender.lower(),
                    'marketing' in sender.lower(),
                    'updates' in sender.lower(),
                    'notifications' in sender.lower(),
                    data['count'] >= 5  # High volume sender
                ])
                
                mailing_lists.append({
                    'sender': sender,
                    'email_count': data['count'],
                    'likely_mailing_list': is_likely_mailing_list,
                    'latest_date': data['latest_date'],
                    'sample_subjects': data['subjects'][:3]  # First 3 subjects as sample
                })
        
        # Sort by email count (descending)
        mailing_lists.sort(key=lambda x: x['email_count'], reverse=True)
        
        return mailing_lists
    except Exception as e:
        return [{"error": f"Failed to analyze mailing list senders: {str(e)}"}]

@mcp.tool()
def create_filter_rule(rule_name: str, conditions: str, actions: str, enabled: bool = True) -> dict:
    """
    Create a new email filtering rule.
    
    Args:
        rule_name: Unique name for the rule
        conditions: JSON string of conditions to match (e.g., '{"from": "newsletter@example.com", "subject_contains": "sale"}')
        actions: JSON string of actions to take (e.g., '{"move_to_folder": "Promotions", "mark_as_read": true}')
        enabled: Whether the rule is active (default: True)
    
    Returns:
        Status of rule creation
    """
    try:
        # Parse JSON strings
        conditions_dict = json.loads(conditions)
        actions_dict = json.loads(actions)
        
        success = email_client.create_filter_rule(rule_name, conditions_dict, actions_dict, enabled)
        
        if success:
            return {
                "status": "success",
                "message": f"Filter rule '{rule_name}' created successfully",
                "rule": {
                    "name": rule_name,
                    "conditions": conditions_dict,
                    "actions": actions_dict,
                    "enabled": enabled
                }
            }
        else:
            return {
                "status": "error",
                "message": f"Failed to create filter rule '{rule_name}'"
            }
    except json.JSONDecodeError as e:
        return {"status": "error", "message": f"Invalid JSON format: {str(e)}"}
    except Exception as e:
        return {"status": "error", "message": f"Error creating filter rule: {str(e)}"}

@mcp.tool()
def list_filter_rules() -> List[dict]:
    """
    Get list of all filtering rules.
    
    Returns:
        List of all filter rules with their details
    """
    try:
        rules = email_client.load_filter_rules()
        return rules
    except Exception as e:
        return [{"error": f"Failed to load filter rules: {str(e)}"}]

@mcp.tool()
def delete_filter_rule(rule_id: str) -> dict:
    """
    Delete a filtering rule by ID.
    
    Args:
        rule_id: ID of the rule to delete
    
    Returns:
        Status of rule deletion
    """
    try:
        success = email_client.delete_filter_rule(rule_id)
        
        if success:
            return {
                "status": "success",
                "message": f"Filter rule with ID '{rule_id}' deleted successfully"
            }
        else:
            return {
                "status": "error",
                "message": f"Failed to delete filter rule with ID '{rule_id}' (rule not found)"
            }
    except Exception as e:
        return {"status": "error", "message": f"Error deleting filter rule: {str(e)}"}

@mcp.tool()
def update_filter_rule(rule_id: str, enabled: bool = None, rule_name: str = None, conditions: str = None, actions: str = None) -> dict:
    """
    Update an existing filtering rule.
    
    Args:
        rule_id: ID of the rule to update
        enabled: Whether the rule should be enabled (optional)
        rule_name: New name for the rule (optional)
        conditions: New conditions JSON string (optional)
        actions: New actions JSON string (optional)
    
    Returns:
        Status of rule update
    """
    try:
        updates = {}
        
        if enabled is not None:
            updates['enabled'] = enabled
        if rule_name is not None:
            updates['name'] = rule_name
        if conditions is not None:
            updates['conditions'] = json.loads(conditions)
        if actions is not None:
            updates['actions'] = json.loads(actions)
        
        if not updates:
            return {"status": "error", "message": "No updates provided"}
        
        success = email_client.update_filter_rule(rule_id, **updates)
        
        if success:
            return {
                "status": "success",
                "message": f"Filter rule with ID '{rule_id}' updated successfully",
                "updates": updates
            }
        else:
            return {
                "status": "error",
                "message": f"Failed to update filter rule with ID '{rule_id}' (rule not found)"
            }
    except json.JSONDecodeError as e:
        return {"status": "error", "message": f"Invalid JSON format: {str(e)}"}
    except Exception as e:
        return {"status": "error", "message": f"Error updating filter rule: {str(e)}"}

@mcp.tool()
def apply_filter_rules(mailbox: str = "INBOX", limit: int = 50) -> dict:
    """
    Apply all enabled filtering rules to emails in a mailbox.
    
    Args:
        mailbox: Mailbox to process (default: INBOX)
        limit: Maximum number of emails to process (default: 50)
    
    Returns:
        Summary of rules applied and actions taken
    """
    try:
        result = email_client.apply_filter_rules(mailbox, limit)
        return result
    except Exception as e:
        return {"error": f"Failed to apply filter rules: {str(e)}"}

@mcp.tool()
def get_filter_rule_examples() -> dict:
    """
    Get examples of common filtering rules to help with rule creation.
    
    Returns:
        Dictionary with example conditions and actions
    """
    return {
        "condition_examples": {
            "from_specific_sender": '{"from": "newsletter@example.com"}',
            "subject_contains": '{"subject_contains": "newsletter"}',
            "subject_equals": '{"subject_equals": "Weekly Report"}',
            "body_contains": '{"body_contains": "unsubscribe"}',
            "sender_domain": '{"sender_domain": "marketing.company.com"}',
            "multiple_conditions": '{"from": "updates@github.com", "subject_contains": "Pull Request"}'
        },
        "action_examples": {
            "move_to_folder": '{"move_to_folder": "Newsletter"}',
            "mark_as_read": '{"mark_as_read": true}',
            "delete_email": '{"delete": true}',
            "move_and_mark": '{"move_to_folder": "Promotions", "mark_as_read": true}',
            "mark_important": '{"mark_as_important": true}'
        },
        "complete_rule_examples": [
            {
                "name": "GitHub Notifications",
                "conditions": '{"sender_domain": "github.com"}',
                "actions": '{"move_to_folder": "GitHub", "mark_as_read": true}'
            },
            {
                "name": "Marketing Emails",
                "conditions": '{"body_contains": "unsubscribe"}',
                "actions": '{"move_to_folder": "Marketing"}'
            },
            {
                "name": "Important Client",
                "conditions": '{"from": "client@importantcompany.com"}',
                "actions": '{"move_to_folder": "VIP", "mark_as_important": true}'
            }
        ]
    }

@mcp.tool()
def bulk_move_emails(email_ids: str, target_folder: str, source_folder: str = "INBOX") -> dict:
    """
    Move multiple emails to a folder efficiently using bulk operations.
    
    Args:
        email_ids: Comma-separated list of email IDs to move
        target_folder: Destination folder name
        source_folder: Source folder name (default: INBOX)
    
    Returns:
        Dictionary with move operation results
    """
    try:
        # Parse email IDs
        id_list = [id.strip() for id in email_ids.split(',') if id.strip()]
        
        if not id_list:
            return {"error": "No valid email IDs provided"}
        
        result = email_client.bulk_move_emails(id_list, target_folder, source_folder)
        return {
            "status": "success",
            "moved": result.get('moved', 0),
            "failed": result.get('failed', 0),
            "total": result.get('total', 0),
            "details": result.get('details', [])
        }
    except Exception as e:
        return {"error": f"Failed to bulk move emails: {str(e)}"}

@mcp.tool()
def bulk_mark_emails_as_read(email_ids: str, mailbox: str = "INBOX", mark_read: bool = True) -> dict:
    """
    Bulk mark emails as read or unread.
    
    Args:
        email_ids: Comma-separated list of email IDs to mark
        mailbox: Mailbox containing the emails (default: INBOX)
        mark_read: True to mark as read, False to mark as unread
    
    Returns:
        Dictionary with marking operation results
    """
    try:
        # Parse email IDs
        id_list = [id.strip() for id in email_ids.split(',') if id.strip()]
        
        if not id_list:
            return {"error": "No valid email IDs provided"}
        
        action = "add" if mark_read else "remove"
        result = email_client.bulk_mark_emails(id_list, '\\Seen', action, mailbox)
        
        return {
            "status": "success",
            "marked": result.get('marked', 0),
            "failed": result.get('failed', 0),
            "total": result.get('total', 0),
            "action": "marked_as_read" if mark_read else "marked_as_unread"
        }
    except Exception as e:
        return {"error": f"Failed to bulk mark emails: {str(e)}"}

@mcp.tool()
def bulk_mark_emails_as_important(email_ids: str, mailbox: str = "INBOX", mark_important: bool = True) -> dict:
    """
    Bulk mark emails as important/flagged or remove importance.
    
    Args:
        email_ids: Comma-separated list of email IDs to mark
        mailbox: Mailbox containing the emails (default: INBOX)
        mark_important: True to mark as important, False to remove importance
    
    Returns:
        Dictionary with marking operation results
    """
    try:
        # Parse email IDs
        id_list = [id.strip() for id in email_ids.split(',') if id.strip()]
        
        if not id_list:
            return {"error": "No valid email IDs provided"}
        
        action = "add" if mark_important else "remove"
        result = email_client.bulk_mark_emails(id_list, '\\Flagged', action, mailbox)
        
        return {
            "status": "success",
            "marked": result.get('marked', 0),
            "failed": result.get('failed', 0),
            "total": result.get('total', 0),
            "action": "marked_as_important" if mark_important else "removed_importance"
        }
    except Exception as e:
        return {"error": f"Failed to bulk mark emails as important: {str(e)}"}

@mcp.tool()
def bulk_delete_emails(email_ids: str, mailbox: str = "INBOX", permanent: bool = False) -> dict:
    """
    Bulk delete emails (move to Trash or permanent deletion).
    
    Args:
        email_ids: Comma-separated list of email IDs to delete
        mailbox: Source mailbox (default: INBOX)
        permanent: If True, permanently delete; if False, move to Trash
    
    Returns:
        Dictionary with deletion results
    """
    try:
        # Parse email IDs
        id_list = [id.strip() for id in email_ids.split(',') if id.strip()]
        
        if not id_list:
            return {"error": "No valid email IDs provided"}
        
        result = email_client.bulk_delete_emails(id_list, mailbox, permanent)
        
        return {
            "status": "success",
            "deleted": result.get('deleted', 0),
            "moved": result.get('moved', 0),  # For non-permanent deletion
            "failed": result.get('failed', 0),
            "total": len(id_list),
            "permanent": permanent
        }
    except Exception as e:
        return {"error": f"Failed to bulk delete emails: {str(e)}"}

@mcp.tool()
def apply_filter_rules_optimized(mailbox: str = "INBOX", limit: int = 200, chunk_size: int = 50) -> dict:
    """
    Apply filtering rules to large numbers of emails using optimized bulk processing.
    
    Args:
        mailbox: Mailbox to process (default: INBOX)
        limit: Maximum number of emails to process (default: 200)
        chunk_size: Size of processing chunks for memory management (default: 50)
    
    Returns:
        Summary of optimized rule application
    """
    try:
        result = email_client.apply_filter_rules_optimized(mailbox, limit, chunk_size)
        return result
    except Exception as e:
        return {"error": f"Failed to apply filter rules (optimized): {str(e)}"}

@mcp.tool()
def bulk_get_emails(email_ids: str, mailbox: str = "INBOX") -> List[dict]:
    """
    Efficiently retrieve multiple emails in bulk.
    
    Args:
        email_ids: Comma-separated list of email IDs to retrieve
        mailbox: Mailbox containing the emails (default: INBOX)
    
    Returns:
        List of email objects with full content
    """
    try:
        # Parse email IDs
        id_list = [id.strip() for id in email_ids.split(',') if id.strip()]
        
        if not id_list:
            return [{"error": "No valid email IDs provided"}]
        
        emails_dict = email_client.get_bulk_emails(id_list, mailbox)
        
        # Convert to list format expected by MCP tools
        emails_list = []
        for email_id in id_list:
            if email_id in emails_dict:
                emails_list.append(emails_dict[email_id])
            else:
                emails_list.append({"id": email_id, "error": "Email not found or could not be retrieved"})
        
        return emails_list
    except Exception as e:
        return [{"error": f"Failed to bulk retrieve emails: {str(e)}"}]

# MCP Resources
@mcp.resource("proton://inbox/summary")
def inbox_summary() -> Resource:
    """Get a summary of your inbox"""
    try:
        recent_emails = email_client.search_emails("ALL", "INBOX", 5)
        summary = f"Recent emails in your Proton inbox:\n\n"
        for email_item in recent_emails:
            summary += f"{email_item['subject']} - From: {email_item['from']} ({email_item['date']})\n"
        
        return Resource(
            uri="proton://inbox/summary",
            name="Inbox Summary",
            description="Summary of recent emails in your Proton inbox",
            mimeType="text/plain",
            text=summary
        )
    except Exception as e:
        return Resource(
            uri="proton://inbox/summary",
            name="Inbox Summary",
            description="Error retrieving inbox summary",
            mimeType="text/plain",
            text=f"Error: {str(e)}"
        )

# Run the server
if __name__ == "__main__":
    mcp.run()