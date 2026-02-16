"""
Encryption utilities for secure token storage
"""

from cryptography.fernet import Fernet
from django.conf import settings
import base64
import hashlib


class TokenEncryption:
    """
    Utility class for encrypting and decrypting access tokens
    Uses Fernet (symmetric encryption) based on SECRET_KEY
    """

    @staticmethod
    def _get_fernet_key():
        """
        Generate Fernet key from Django SECRET_KEY
        """
        # Fernet key must be 32 url-safe base64-encoded bytes
        key = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
        return base64.urlsafe_b64encode(key)

    @classmethod
    def encrypt(cls, plaintext: str) -> str:
        """
        Encrypt a plaintext string

        Args:
            plaintext: String to encrypt (e.g., access token)

        Returns:
            Encrypted string (base64 encoded)
        """
        if not plaintext:
            return ""

        fernet = Fernet(cls._get_fernet_key())
        encrypted = fernet.encrypt(plaintext.encode())
        return encrypted.decode()

    @classmethod
    def decrypt(cls, encrypted: str) -> str:
        """
        Decrypt an encrypted string

        Args:
            encrypted: Encrypted string

        Returns:
            Decrypted plaintext string
        """
        if not encrypted:
            return ""

        fernet = Fernet(cls._get_fernet_key())
        decrypted = fernet.decrypt(encrypted.encode())
        return decrypted.decode()


class EncryptedTextField:
    """
    Custom field descriptor for transparent encryption/decryption
    Usage:
        class MyModel(models.Model):
            _encrypted_token = models.TextField()
            token = EncryptedTextField('_encrypted_token')
    """

    def __init__(self, field_name):
        self.field_name = field_name

    def __get__(self, instance, owner):
        if instance is None:
            return self

        encrypted_value = getattr(instance, self.field_name)
        if not encrypted_value:
            return ""
        return TokenEncryption.decrypt(encrypted_value)

    def __set__(self, instance, value):
        if not value:
            encrypted_value = ""
        else:
            encrypted_value = TokenEncryption.encrypt(value)
        setattr(instance, self.field_name, encrypted_value)
