from django.http import HttpResponse
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.conf import settings

from .models import Order, OrderLineItem
from products.models import Product
from profiles.models import UserProfile

import json
import time


class StripeWH_Handler:
    """Handle Stripe webhooks safely"""

    def __init__(self, request):
        self.request = request

    def _send_confirmation_email(self, order, email_to_send=None):
        """Send the user a confirmation email safely"""
        try:
            cust_email = order.email or email_to_send or getattr(order.user_profile.user, 'email', None)
            subject = render_to_string(
                'checkout/confirmation_emails/confirmation_email_subject.txt',
                {'order': order})
            body = render_to_string(
                'checkout/confirmation_emails/confirmation_email_body.txt',
                {'order': order, 'contact_email': settings.DEFAULT_FROM_EMAIL})

            send_mail(
                subject,
                body,
                settings.DEFAULT_FROM_EMAIL,
                [cust_email]
            )

        except Exception as e:
            print(f"Error sending confirmation email: {e}")

    def handle_event(self, event):
        """Handle unexpected or generic webhook events"""
        return HttpResponse(
            content=f'Unhandled webhook received: {event["type"]}',
            status=200
        )

    def handle_payment_intent_succeeded(self, event):
        """Handle the payment_intent.succeeded webhook from Stripe"""
        intent = event.data.object
        pid = getattr(intent, "id", None)

        # Safely get metadata
        metadata = getattr(intent, "metadata", {}) or {}
        bag = metadata.get("bag") or "{}"
        save_info = metadata.get("save_info") in ['true', True]
        username = metadata.get("username") or "AnonymousUser"
        email_to_send = metadata.get("email") or None

        # Safely get billing info
        charges = getattr(intent, "charges", None)
        if charges and getattr(charges, "data", None):
            charge = charges.data[0]
            billing_details = getattr(charge, "billing_details", {}) or {}
            grand_total = round(getattr(charge, "amount", 0) / 100, 2)
        else:
            billing_details = getattr(intent, "billing_details", {}) or {}
            grand_total = round(getattr(intent, "amount_received", 0) / 100, 2)

        # Safely get shipping info
        shipping_details = getattr(intent, "shipping", {}) or {}
        address = getattr(shipping_details, "address", {}) or {}

        # Clean empty fields
        for field, value in address.items():
            if value == "":
                address[field] = None

        # Update user profile if save_info is True
        profile = None
        if username != "AnonymousUser":
            try:
                profile = UserProfile.objects.get(user__username=username)
                if save_info:
                    profile.default_phone_number = getattr(shipping_details, "phone", "") or profile.default_phone_number
                    profile.default_country = address.get("country", "") or profile.default_country
                    profile.default_postcode = address.get("postal_code", "") or profile.default_postcode
                    profile.default_town_or_city = address.get("city", "") or profile.default_town_or_city
                    profile.default_street_address1 = address.get("line1", "") or profile.default_street_address1
                    profile.default_street_address2 = address.get("line2", "") or profile.default_street_address2
                    profile.default_county = address.get("state", "") or profile.default_county
                    profile.save()
            except UserProfile.DoesNotExist:
                profile = None

        # Try to find existing order
        order = None
        order_exists = False
        attempt = 1
        while attempt <= 5:
            try:
                order = Order.objects.get(
                    full_name__iexact=getattr(shipping_details, "name", "") or "",
                    email__iexact=getattr(billing_details, "email", "") or "",
                    phone_number__iexact=getattr(shipping_details, "phone", "") or "",
                    country__iexact=address.get("country", "") or "",
                    postcode__iexact=address.get("postal_code", "") or "",
                    town_or_city__iexact=address.get("city", "") or "",
                    street_address1__iexact=address.get("line1", "") or "",
                    street_address2__iexact=address.get("line2", "") or "",
                    county__iexact=address.get("state", "") or "",
                    grand_total=grand_total,
                    original_bag=bag,
                    stripe_pid=pid,
                )
                order_exists = True
                break
            except Order.DoesNotExist:
                attempt += 1
                time.sleep(1)

        if order_exists:
            self._send_confirmation_email(order)
            return HttpResponse(
                content=f'Webhook received: {event["type"]} | SUCCESS: Verified order already in database',
                status=200
            )

        # Otherwise, create a new order
        try:
            order = Order.objects.create(
                full_name=getattr(shipping_details, "name", "") or "",
                user_profile=profile,
                email=getattr(billing_details, "email", "") or "",
                phone_number=getattr(shipping_details, "phone", "") or "",
                country=address.get("country", "") or "",
                postcode=address.get("postal_code", "") or "",
                town_or_city=address.get("city", "") or "",
                street_address1=address.get("line1", "") or "",
                street_address2=address.get("line2", "") or "",
                county=address.get("state", "") or "",
                grand_total=grand_total,
                original_bag=bag,
                stripe_pid=pid,
            )

            # Safely parse bag JSON
            try:
                bag_data = json.loads(bag)
            except json.JSONDecodeError:
                bag_data = {}

            for item_id, item_data in bag_data.items():
                try:
                    product = Product.objects.get(id=item_id)
                    if isinstance(item_data, int):
                        OrderLineItem.objects.create(
                            order=order,
                            product=product,
                            quantity=item_data
                        )
                    else:
                        for size, quantity in item_data.get("items_by_size", {}).items():
                            OrderLineItem.objects.create(
                                order=order,
                                product=product,
                                quantity=quantity,
                                product_size=size
                            )
                except Product.DoesNotExist:
                    print(f"Product not found for id {item_id}, skipping line item.")

        except Exception as e:
            if order:
                order.delete()
            return HttpResponse(
                content=f'Webhook received: {event["type"]} | ERROR creating order: {e}',
                status=500
            )

        self._send_confirmation_email(order, email_to_send)

        return HttpResponse(
            content=f'Webhook received: {event["type"]} | SUCCESS: Created order in webhook',
            status=200
        )

    def handle_payment_intent_payment_failed(self, event):
        """Handle payment_intent.payment_failed webhook"""
        return HttpResponse(
            content=f'Webhook received: {event["type"]}',
            status=200
        )
