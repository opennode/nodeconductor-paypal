import datetime
import decimal
import urlparse
import dateutil.parser
import paypalrestsdk as paypal

from django.conf import settings
from django.utils import six, timezone


class PayPalError(Exception):
    pass


class PaypalPayment(object):
    def __init__(self, payment_id, approval_url, token):
        self.payment_id = payment_id
        self.approval_url = approval_url
        self.token = token


class PaypalBackend(object):

    def __init__(self):
        config = settings.NODECONDUCTOR_PAYPAL['BACKEND']
        self.configure(**config)

    def configure(self, mode, client_id, client_secret, currency_name, **kwargs):
        # extra method to validate required config options
        self.currency_name = currency_name

        paypal.configure({
            'mode': mode,
            'client_id': client_id,
            'client_secret': client_secret
        })

    def _find_approval_url(self, links):
        for link in links:
            if link.rel == 'approval_url':
                return link.href
        raise PayPalError('Approval URL is not found')

    def _find_token(self, approval_url):
        parts = urlparse.urlparse(approval_url)
        params = urlparse.parse_qs(parts.query)
        token = params.get('token')
        if not token:
            raise PayPalError('Unable to parse token from approval_url')
        return token[0]

    def make_payment(self, total, subtotal, tax, description, return_url, cancel_url):
        """
        Make PayPal single using Express Checkout workflow.
        :param total: Decimal value of payment including VAT tax
        :param subtotal: Decimal value of payment without VAT tax
        :param tax: Decimal value of VAT tax
        :param description: Description of payment
        :param return_url: Callback view URL for approved payment
        :param cancel_url: Callback view URL for cancelled payment
        :return: Object containing backend payment id, approval URL and token.
        """
        payment = paypal.Payment({
            'intent': 'sale',
            'payer': {'payment_method': 'paypal'},
            'transactions': [
                {
                    'amount': {
                        'total': str(total),  # serialize decimal
                        'currency': self.currency_name,
                        'details': {
                            'subtotal': str(subtotal),
                            'tax': str(tax)
                        }
                    },
                    'description': description
                }
            ],
            'redirect_urls': {
                'return_url': return_url,
                'cancel_url': cancel_url
            }
        })

        try:
            if payment.create():
                approval_url = self._find_approval_url(payment.links)
                token = self._find_token(approval_url)
                return PaypalPayment(payment.id, approval_url, token)
            else:
                raise PayPalError(payment.error)
        except paypal.exceptions.ConnectionError as e:
            six.reraise(PayPalError, e)

    def approve_payment(self, payment_id, payer_id):
        try:
            payment = paypal.Payment.find(payment_id)
            # When payment is not found PayPal returns empty result instead of raising an exception
            if not payment:
                raise PayPalError('Payment not found')
            if payment.execute({'payer_id': payer_id}):
                return True
            else:
                raise PayPalError(payment.error)
        except paypal.exceptions.ConnectionError as e:
            six.reraise(PayPalError, e)

    def create_plan(self, amount, name, description, return_url, cancel_url):
        """
        Create and activate monthly billing plan using PayPal Rest API.
        On success returns plan_id
        """
        plan = paypal.BillingPlan({
            'name': name,
            'description': description,
            'type': 'INFINITE',
            'payment_definitions': [{
                'name': 'Monthly payment for {}'.format(name),
                'type': 'REGULAR',
                'frequency_interval': 1,
                'frequency': 'MONTH',
                'cycles': 0,
                'amount': {
                    'currency': self.currency_name,
                    'value': str(amount)
                }
            }],
            'merchant_preferences': {
                'return_url': return_url,
                'cancel_url': cancel_url,
                'auto_bill_amount': 'YES',
            }
        })

        try:
            if plan.create() and plan.activate():
                return plan.id
            else:
                raise PayPalError(plan.error)
        except paypal.exceptions.ConnectionError as e:
            six.reraise(PayPalError, e)

    def create_agreement(self, plan_id, name):
        """
        Create billing agreement. On success returns approval_url and token
        """
        # PayPal does not support immediate start of agreement
        # That's why we need to increase start date by small amount of time
        start_date = datetime.datetime.utcnow() + datetime.timedelta(minutes=1)

        # PayPal does not fully support ISO 8601 format
        formatted_date = start_date.strftime('%Y-%m-%dT%H:%M:%SZ')

        agreement = paypal.BillingAgreement({
            'name': name,
            'description': 'Agreement for {}'.format(name),
            'start_date': formatted_date,
            'payer': {'payment_method': 'paypal'},
            'plan': {'id': plan_id}
        })
        try:
            if agreement.create():
                approval_url = self._find_approval_url(agreement.links)

                # PayPal does not return agreement ID until it is approved
                # That's why we need to extract token in order to identify it with agreement in DB
                token = self._find_token(approval_url)
                return approval_url, token
            else:
                raise PayPalError(agreement.error)
        except paypal.exceptions.ConnectionError as e:
            six.reraise(PayPalError, e)

    def execute_agreement(self, payment_token):
        """
        Agreement should be executed if user has approved it.
        On success returns agreement id
        """
        try:
            agreement = paypal.BillingAgreement.execute(payment_token)
            if not agreement:
                raise PayPalError('Can not execute agreement')
            return agreement.id
        except paypal.exceptions.ConnectionError as e:
            six.reraise(PayPalError, e)

    def get_agreement(self, agreement_id):
        """
        Get agreement from PayPal by ID
        """
        try:
            agreement = paypal.BillingAgreement.find(agreement_id)
            # When agreement is not found PayPal returns empty result instead of raising an exception
            if not agreement:
                raise PayPalError('Agreement not found')
            return agreement
        except paypal.exceptions.ConnectionError as e:
            six.reraise(PayPalError, e)

    def cancel_agreement(self, agreement_id):
        agreement = self.get_agreement(agreement_id)

        try:
            # Because user may cancel agreement via PayPal web UI
            # we need to distinguish it from cancel done via API
            if agreement.cancel({'note': 'Canceling the agreement by application'}):
                return True
            else:
                raise PayPalError(agreement.error)
        except paypal.exceptions.ConnectionError as e:
            six.reraise(PayPalError, e)

    def get_agreement_transactions(self, agreement_id, start_date, end_date=None):
        if not end_date:
            end_date = timezone.now()

        # If start and end date are the same PayPal raises exceptions
        # That's why we need to increase end_date by one day
        if end_date - start_date < datetime.timedelta(days=1):
            end_date += datetime.timedelta(days=1)

        formatted_start_date = start_date.strftime('%Y-%m-%d')
        formatted_end_date = end_date.strftime('%Y-%m-%d')

        agreement = self.get_agreement(agreement_id)
        try:
            data = agreement.search_transactions(formatted_start_date, formatted_end_date)
            txs = data.agreement_transaction_list
            if not txs:
                return []

            results = []
            for tx in txs:
                if tx.status != 'Completed':
                    continue
                results.append({
                    'time_stamp': dateutil.parser.parse(tx.time_stamp),
                    'transaction_id': tx.transaction_id,
                    'amount': decimal.Decimal(tx.amount.value),
                    'payer_email': tx.payer_email
                })
            return results

        except paypal.exceptions.ConnectionError as e:
            six.reraise(PayPalError, e)
