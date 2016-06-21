from __future__ import unicode_literals

import logging
import os
from StringIO import StringIO

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import models
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.encoding import python_2_unicode_compatible
from django_fsm import transition, FSMIntegerField
from model_utils.models import TimeStampedModel
from xhtml2pdf.pisa import pisaDocument

from nodeconductor.core.models import UuidMixin, ErrorMessageMixin
from nodeconductor.logging.loggers import LoggableMixin
from nodeconductor.structure.models import Customer


logger = logging.getLogger(__name__)


@python_2_unicode_compatible
class Payment(LoggableMixin, TimeStampedModel, UuidMixin, ErrorMessageMixin):
    class Meta:
        ordering = ['-modified']

    class Permissions(object):
        customer_path = 'customer'

    class States(object):
        INIT = 0
        CREATED = 1
        APPROVED = 2
        CANCELLED = 3
        ERRED = 4

    STATE_CHOICES = (
        (States.INIT, 'Initial'),
        (States.CREATED, 'Created'),
        (States.APPROVED, 'Approved'),
        (States.ERRED, 'Erred'),
    )

    state = FSMIntegerField(default=States.INIT, choices=STATE_CHOICES)

    customer = models.ForeignKey(Customer)
    amount = models.DecimalField(max_digits=9, decimal_places=2)
    tax = models.DecimalField(max_digits=9, decimal_places=2, default=0)

    # Payment ID is persistent identifier of payment
    backend_id = models.CharField(max_length=255, null=True)

    # Token is temporary identifier of payment
    token = models.CharField(max_length=255, null=True)

    # URL is fetched from backend
    approval_url = models.URLField()

    def __str__(self):
        return "%s %.2f %s" % (self.modified, self.amount, self.customer.name)

    def get_log_fields(self):
        return ('uuid', 'customer', 'amount', 'modified', 'status')

    @transition(field=state, source=States.INIT, target=States.CREATED)
    def set_created(self):
        pass

    @transition(field=state, source=States.CREATED, target=States.APPROVED)
    def set_approved(self):
        pass

    @transition(field=state, source=States.CREATED, target=States.CANCELLED)
    def set_cancelled(self):
        pass

    @transition(field=state, source='*', target=States.ERRED)
    def set_erred(self):
        pass


@python_2_unicode_compatible
class Invoice(LoggableMixin, UuidMixin):
    class Meta:
        ordering = ['-start_date']

    class Permissions(object):
        customer_path = 'customer'

    customer = models.ForeignKey(Customer, related_name='paypal_invoices')
    start_date = models.DateField()
    end_date = models.DateField()
    pdf = models.FileField(upload_to='paypal-invoices', blank=True, null=True)

    @property
    def total_amount(self):
        """ Get total price of all items excluding VAT tax """
        return sum(item.amount for item in self.items)

    @property
    def total_tax(self):
        """ Get total price of all items' VAT tax """
        return sum(item.tax for item in self.items)

    def get_log_fields(self):
        return ('uuid', 'customer', 'total_amount', 'start_date', 'end_date')

    def generate_invoice_file_name(self):
        return '{}-invoice-{}.pdf'.format(self.start_date.strftime('%Y-%m-%d'), self.pk)

    def generate_pdf(self):
        # cleanup if pdf already existed
        if self.pdf is not None:
            self.pdf.delete()

        info = settings.NODECONDUCTOR_PAYPAL.get('INVOICE', {})
        logo = info.get('logo', None)
        if logo and not logo.startswith('/'):
            logo = os.path.join(settings.BASE_DIR, logo)

        currency = settings.NODECONDUCTOR_PAYPAL['BACKEND']['currency_name']

        html = render_to_string('nodeconductor_paypal/invoice.html', {
            'invoice': self,
            'invoice_date': timezone.now(),
            'currency': currency,
            'info': info,
            'logo': logo
        })

        result = StringIO()
        pdf = pisaDocument(StringIO(html), result)
        self.pdf.save(self.generate_invoice_file_name(), ContentFile(result.getvalue()))
        if pdf.err:
            logger.error('Unable to save PDF to file: %s', pdf.err)
        else:
            self.save(update_fields=['pdf'])

    def __str__(self):
        return "Invoice #%s" % self.id


class InvoiceItem(models.Model):
    """
    Invoice item corresponds to transaction of payment or billing plan agreement
    """
    class Meta:
        ordering = ['invoice', '-created_at']

    invoice = models.ForeignKey(Invoice, related_name='items')
    amount = models.DecimalField(max_digits=9, decimal_places=2)
    tax = models.DecimalField(max_digits=9, decimal_places=2, default=0)
    description = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    backend_id = models.CharField(max_length=255, blank=True, null=True)
