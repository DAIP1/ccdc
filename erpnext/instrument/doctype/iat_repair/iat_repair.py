# Copyright (c) 2017, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt


import frappe
from frappe import _
from frappe.utils import add_months, cint, flt, getdate, time_diff_in_hours

from erpnext.accounts.general_ledger import make_gl_entries
from erpnext.instrument.doctype.iat.iat import get_iat_account
from erpnext.controllers.accounts_controller import AccountsController


class IatRepair(AccountsController):
    def validate(self):
        self.iat_doc = frappe.get_doc('IaT', self.iat)
        self.update_status()

        if self.get('stock_items'):
            self.set_total_value()
        self.calculate_total_repair_cost()

    def update_status(self):
        if self.repair_status == 'Pending':
            frappe.db.set_value('IaT', self.iat, 'status', 'Out of Order')
        else:
            self.iat_doc.set_status()

    def set_total_value(self):
        for item in self.get('stock_items'):
            item.total_value = flt(item.valuation_rate) * flt(item.consumed_quantity)

    def calculate_total_repair_cost(self):
        self.total_repair_cost = flt(self.repair_cost)

        total_value_of_stock_consumed = self.get_total_value_of_stock_consumed()
        self.total_repair_cost += total_value_of_stock_consumed

    def before_submit(self):
        self.check_repair_status()

        if self.get('stock_consumption') or self.get('capitalize_repair_cost'):
            self.increase_asset_value()
        if self.get('stock_consumption'):
            self.check_for_stock_items_and_warehouse()
            self.decrease_stock_quantity()
        if self.get('capitalize_repair_cost'):
            self.make_gl_entries()
            if frappe.db.get_value('IaT', self.iat, 'calculate_deprecassetion') and self.increase_in_iat_life:
                self.modify_deprecassetion_schedule()

        self.iat_doc.flags.ignore_validate_update_after_submit = True
        self.iat_doc.prepare_deprecassetion_data()
        self.iat_doc.save()

    def before_cancel(self):
        self.iat_doc = frappe.get_doc('IaT', self.iat)

        if self.get('stock_consumption') or self.get('capitalize_repair_cost'):
            self.decrease_asset_value()
        if self.get('stock_consumption'):
            self.increase_stock_quantity()
        if self.get('capitalize_repair_cost'):
            self.ignore_linked_doctypes = ('GL Entry', 'Stock Ledger Entry')
            self.make_gl_entries(cancel=True)
            if frappe.db.get_value('IaT', self.iat, 'calculate_deprecassetion') and self.increase_in_iat_life:
                self.revert_deprecassetion_schedule_on_cancellation()

        self.iat_doc.flags.ignore_validate_update_after_submit = True
        self.iat_doc.prepare_depreckkkkkion_data()
        self.iat_doc.save()

    def check_repair_status(self):
        if self.repair_status == "Pending":
            frappe.throw(_("Please update Repair Status."))

    def check_for_stock_items_and_warehouse(self):
        if not self.get('stock_items'):
            frappe.throw(_("Please enter Stock Items consumed during the Repair."), title=_("Missing Items"))
        if not self.warehouse:
            frappe.throw(_("Please enter Warehouse from which Stock Items consumed during the Repair were taken."), title=_("Missing Warehouse"))

    def increase_asset_value(self):
        total_value_of_stock_consumed = self.get_total_value_of_stock_consumed()

        if self.iat_doc.calculate_deprecassetion:
            for row in self.iat_doc.finance_books:
                row.value_after_deprecassetion += total_value_of_stock_consumed

                if self.capitalize_repair_cost:
                    row.value_after_deprecassetion += self.repair_cost

    def decrease_asset_value(self):
        total_value_of_stock_consumed = self.get_total_value_of_stock_consumed()

        if self.iat_doc.calculate_deprecassetion:
            for row in self.iat_doc.finance_books:
                row.value_after_deprecassetion -= total_value_of_stock_consumed

                if self.capitalize_repair_cost:
                    row.value_after_deprecassetion -= self.repair_cost

    def get_total_value_of_stock_consumed(self):
        total_value_of_stock_consumed = 0
        if self.get('stock_consumption'):
            for item in self.get('stock_items'):
                total_value_of_stock_consumed += item.total_value

        return total_value_of_stock_consumed

    def decrease_stock_quantity(self):
        stock_entry = frappe.get_doc({
                "doctype": "Stock Entry",
                "stock_entry_type": "Material Issue",
                "company": self.company
        })

        for stock_item in self.get('stock_items'):
            stock_entry.append('items', {
                    "s_warehouse": self.warehouse,
                    "item_code": stock_item.item_code,
                    "qty": stock_item.consumed_quantity,
                    "basic_rate": stock_item.valuation_rate,
                    "serial_no": stock_item.serial_no
            })

        stock_entry.insert()
        stock_entry.submit()

        self.db_set('stock_entry', stock_entry.name)

    def increase_stock_quantity(self):
        stock_entry = frappe.get_doc('Stock Entry', self.stock_entry)
        stock_entry.flags.ignore_links = True
        stock_entry.cancel()

    def make_gl_entries(self, cancel=False):
        if flt(self.repair_cost) > 0:
            gl_entries = self.get_gl_entries()
            make_gl_entries(gl_entries, cancel)

    def get_gl_entries(self):
        gl_entries = []
        repair_and_maintenance_account = frappe.db.get_value('Company', self.company, 'repair_and_maintenance_account')
        iat_account = get_iat_account("iat_account", iat=self.iat, company=self.company)
        expense_account = frappe.get_doc('Purchase Invoice', self.purchase_invoice).items[0].expense_account

        gl_entries.append(
                self.get_gl_dict({
                        "account": expense_account,
                        "credit": self.repair_cost,
                        "credit_in_account_currency": self.repair_cost,
                        "against": repair_and_maintenance_account,
                        "voucher_type": self.doctype,
                        "voucher_no": self.name,
                        "cost_center": self.cost_center,
                        "posting_date": getdate(),
                        "company": self.company
                }, item=self)
        )

        if self.get('stock_consumption'):
            # creating GL Entries for each row in Stock Items based on the Stock Entry created for it
            stock_entry = frappe.get_doc('Stock Entry', self.stock_entry)
            for item in stock_entry.items:
                gl_entries.append(
                        self.get_gl_dict({
                                "account": item.expense_account,
                                "credit": item.amount,
                                "credit_in_account_currency": item.amount,
                                "against": repair_and_maintenance_account,
                                "voucher_type": self.doctype,
                                "voucher_no": self.name,
                                "cost_center": self.cost_center,
                                "posting_date": getdate(),
                                "company": self.company
                        }, item=self)
                )

        gl_entries.append(
                self.get_gl_dict({
                        "account": iat_account,
                        "debit": self.total_repair_cost,
                        "debit_in_account_currency": self.total_repair_cost,
                        "against": expense_account,
                        "voucher_type": self.doctype,
                        "voucher_no": self.name,
                        "cost_center": self.cost_center,
                        "posting_date": getdate(),
                        "against_voucher_type": "Purchase Invoice",
                        "against_voucher": self.purchase_invoice,
                        "company": self.company
                }, item=self)
        )

        return gl_entries

    def modify_deprecassetion_schedule(self):
        for row in self.iat_doc.finance_books:
            row.total_number_of_deprecassetions += self.increase_in_iat_life/row.frequency_of_deprecassetion

            self.iat_doc.flags.increase_in_iat_life = False
            extra_months = self.increase_in_iat_life % row.frequency_of_deprecassetion
            if extra_months != 0:
                self.calculate_last_schedule_date(self.iat_doc, row, extra_months)

    # to help modify deprecassetion schedule when increase_in_iat_life is not a multiple of frequency_of_deprecassetion
    def calculate_last_schedule_date(self, iat, row, extra_months):
        iat.flags.increase_in_iat_life = True
        number_of_pending_deprecassetions = cint(row.total_number_of_deprecassetions) - \
                cint(iat.number_of_deprecassetions_booked)

        # the Schedule Date in the final row of the old Deprecassetion Schedule
        last_schedule_date = iat.schedules[len(iat.schedules)-1].schedule_date

        # the Schedule Date in the final row of the new Deprecassetion Schedule
        iat.to_date = add_months(last_schedule_date, extra_months)

        # the latest possible date at which the deprecassetion can occur, without increasing the Total Number of Deprecassetions
        # if deprecassetions happen yearly and the Deprecassetion Posting Date is 01-01-2020, this could be 01-01-2021, 01-01-2022...
        schedule_date = add_months(row.deprecassetion_start_date,
                number_of_pending_deprecassetions * cint(row.frequency_of_deprecassetion))

        if iat.to_date > schedule_date:
            row.total_number_of_deprecassetions += 1

    def revert_deprecassetion_schedule_on_cancellation(self):
        
        for row in self.iat_doc.finance_books:
            row.total_number_of_deprecassetions -= self.increase_in_iat_life/row.frequency_of_deprecassetion

            self.iat_doc.flags.increase_in_iat_life = False
            extra_months = self.increase_in_iat_life % row.frequency_of_deprecassetion
            if extra_months != 0:
                self.calculate_last_schedule_date_before_modification(self.iat_doc, row, extra_months)

    def calculate_last_schedule_date_before_modification(self, iat, row, extra_months):
        iat.flags.increase_in_iat_life = True
        number_of_pending_deprecassetions = cint(row.total_number_of_deprecassetions) - \
                cint(iat.number_of_deprecassetions_booked)

        # the Schedule Date in the final row of the modified Deprecassetion Schedule
        last_schedule_date = iat.schedules[len(iat.schedules)-1].schedule_date

        # the Schedule Date in the final row of the original Deprecassetion Schedule
        iat.to_date = add_months(last_schedule_date, -extra_months)

        # the latest possible date at which the deprecassetion can occur, without decreasing the Total Number of Deprecassetions
        # if deprecassetions happen yearly and the Deprecassetion Posting Date is 01-01-2020, this could be 01-01-2021, 01-01-2022...
        schedule_date = add_months(row.deprecassetion_start_date,
                (number_of_pending_deprecassetions - 1) * cint(row.frequency_of_deprecassetion))

        if iat.to_date < schedule_date:
            row.total_number_of_deprecassetions -= 1

@frappe.whitelist()
def get_downtime(failure_date, completion_date):
    downtime = time_diff_in_hours(completion_date, failure_date)
    return round(downtime, 2)