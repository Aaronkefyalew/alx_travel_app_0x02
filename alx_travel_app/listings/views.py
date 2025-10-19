import os
import uuid
import requests
from decimal import Decimal
from django.conf import settings
from django.urls import reverse
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response
from .models import Payment
from .serializers import PaymentInitiateSerializer, PaymentSerializer
from .tasks import send_payment_confirmation

CHAPA_BASE = 'https://api.chapa.co/v1'

@api_view(['POST'])
def initiate_payment(request):
    serializer = PaymentInitiateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    tx_ref = f"TRX_{uuid.uuid4().hex[:24]}"
    amount = str(data['amount'])
    payload = {
        'amount': amount,
        'currency': data.get('currency', 'ETB'),
        'email': data['email'],
        'first_name': data['full_name'],
        'last_name': '',
        'phone_number': data.get('phone_number', ''),
        'tx_ref': tx_ref,
        'callback_url': os.getenv('CHAPA_CALLBACK_URL', ''),
        'return_url': os.getenv('CHAPA_RETURN_URL', ''),
        'customization[title]': 'ALX Travel Booking',
        'customization[description]': 'Payment for travel booking',
    }

    headers = {
        'Authorization': f"Bearer {os.getenv('CHAPA_SECRET_KEY', '')}",
        'Content-Type': 'application/json',
    }

    r = requests.post(f"{CHAPA_BASE}/transaction/initialize", json=payload, headers=headers, timeout=30)
    if r.status_code != 200:
        return Response({'detail': 'Failed to initiate payment', 'error': r.text}, status=status.HTTP_502_BAD_GATEWAY)

    resp = r.json()
    if not resp.get('status'):
        return Response({'detail': 'Chapa init failed', 'error': resp}, status=status.HTTP_502_BAD_GATEWAY)

    checkout_url = resp.get('data', {}).get('checkout_url')
    payment = Payment.objects.create(
        tx_ref=tx_ref,
        amount=Decimal(amount),
        currency=payload['currency'],
        email=payload['email'],
        full_name=data['full_name'],
        phone_number=payload['phone_number'],
        status=Payment.STATUS_PENDING,
        checkout_url=checkout_url,
    )

    return Response({'tx_ref': tx_ref, 'checkout_url': checkout_url, 'payment': PaymentSerializer(payment).data})

@api_view(['GET'])
def verify_payment(request):
    tx_ref = request.query_params.get('tx_ref')
    if not tx_ref:
        return Response({'detail': 'tx_ref is required'}, status=status.HTTP_400_BAD_REQUEST)

    headers = {
        'Authorization': f"Bearer {os.getenv('CHAPA_SECRET_KEY', '')}",
    }
    r = requests.get(f"{CHAPA_BASE}/transaction/verify/{tx_ref}", headers=headers, timeout=30)
    if r.status_code != 200:
        return Response({'detail': 'Failed to verify payment', 'error': r.text}, status=status.HTTP_502_BAD_GATEWAY)

    resp = r.json()
    status_str = resp.get('data', {}).get('status')

    try:
        payment = Payment.objects.get(tx_ref=tx_ref)
    except Payment.DoesNotExist:
        return Response({'detail': 'Payment not found'}, status=status.HTTP_404_NOT_FOUND)

    if status_str == 'success':
        payment.status = Payment.STATUS_COMPLETED
        payment.transaction_id = resp.get('data', {}).get('tx_ref') or payment.transaction_id
        payment.save(update_fields=['status', 'transaction_id', 'updated_at'])
        # send email asynchronously
        send_payment_confirmation.delay(payment.email, payment.tx_ref, str(payment.amount))
    elif status_str in {'failed', 'cancelled'}:
        payment.status = Payment.STATUS_FAILED
        payment.save(update_fields=['status', 'updated_at'])

    return Response({'payment': PaymentSerializer(payment).data, 'chapa': resp})
