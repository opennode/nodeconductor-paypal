import logging
from datetime import timedelta, datetime

from django.conf import settings
from django.utils import timezone

from nodeconductor.core import tasks as core_tasks
from nodeconductor.structure import SupportedServices

from . import models, executors

logger = logging.getLogger(__name__)


class DebitCustomers(core_tasks.BackgroundTask):
    """ Fetch a list of shared services (services based on shared settings).
        Calculate the amount of consumed resources "yesterday" (make sure this task executed only once a day)
        Reduce customer's balance accordingly
        Stop online resource if needed
    """
    name = 'paypal.DebitCustomers'

    def is_equal(self, other_task, *args, **kwargs):
        return self.name == other_task.get('name')

    def run(self):
        date = datetime.now() - timedelta(days=1)
        start_date = date.replace(hour=0, minute=0, second=0, microsecond=0)
        end_date = start_date + timedelta(days=1, microseconds=-1)

        # XXX: it's just a placeholder, it doesn't work properly now nor implemented anyhow
        #      perhaps it should merely use price estimates..

        models = SupportedServices.get_resource_models().values()

        for model in models:
            resources = model.objects.filter(
                service_project_link__service__settings__shared=True)

            for resource in resources:
                try:
                    data = resource.get_cost(start_date, end_date)
                except NotImplementedError:
                    continue
                else:
                    resource.customer.debit_account(data['total_amount'])


class PaymentsCleanUp(core_tasks.BackgroundTask):
    name = 'paypal.PaymentsCleanUp'

    def is_equal(self, other_task, *args, **kwargs):
        return self.name == other_task.get('name')

    def run(self):
        timespan = settings.NODECONDUCTOR_PAYPAL.get('STALE_PAYMENTS_LIFETIME', timedelta(weeks=1))
        models.Payment.objects.filter(state=models.Payment.States.CREATED, created__lte=timezone.now() - timespan).delete()


class SendInvoices(core_tasks.BackgroundTask):
    name = 'paypal.SendInvoices'

    def is_equal(self, other_task, *args, **kwargs):
        return self.name == other_task.get('name')

    def run(self):
        new_invoices = models.Invoice.objects.filter(backend_id='')

        for invoice in new_invoices.iterator():
            executors.InvoiceCreateExecutor.execute(invoice)
