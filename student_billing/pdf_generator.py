"""
PDF Generator for Student Bills
Enhanced version with metadata and page numbering
"""
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image, PageBreak
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
from reportlab.pdfgen import canvas
from io import BytesIO
from decimal import Decimal
from django.conf import settings
import os


class NumberedCanvas(canvas.Canvas):
    """Custom canvas to add page numbers at the bottom center"""
    
    def __init__(self, *args, **kwargs):
        canvas.Canvas.__init__(self, *args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        """Add page numbers to each page"""
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_number(num_pages)
            canvas.Canvas.showPage(self)
        canvas.Canvas.save(self)

    def draw_page_number(self, page_count):
        """Draw page number at bottom center"""
        self.setFont("Helvetica", 9)
        self.setFillColor(colors.grey)
        page_num = f"Page {self._pageNumber} of {page_count}"
        # Center the page number
        self.drawCentredString(letter[0] / 2.0, 0.5 * inch, page_num)


class BillPDFGenerator:
    """Generate PDF for student bills with metadata and page numbers"""
    
    def __init__(self, student_bill):
        self.bill = student_bill
        self.buffer = BytesIO()
        
    def generate(self):
        """Generate the PDF and return the buffer"""
        # Create the PDF document with metadata
        doc = SimpleDocTemplate(
            self.buffer,
            pagesize=letter,
            rightMargin=0.5*inch,
            leftMargin=0.5*inch,
            topMargin=0.5*inch,
            bottomMargin=0.75*inch,  # Increased bottom margin for page numbers
            title=f"School Fee Bill - {self.bill.first_name} {self.bill.last_name} {self.bill.billing_template.class_name} {self.bill.billing_template.get_term_display()} ",
            author="School Management System",
            subject=f"Fee Bill for {self.bill.first_name} {self.bill.last_name}",
            creator="School Billing System",
            producer="ReportLab PDF Generator"
        )
        
        # Container for PDF elements
        elements = []
        
        # Styles
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=24,
            textColor=colors.HexColor('#2c3e50'),
            spaceAfter=30,
            alignment=TA_CENTER,
            fontName='Helvetica-Bold'
        )
        
        heading_style = ParagraphStyle(
            'CustomHeading',
            parent=styles['Heading2'],
            fontSize=14,
            textColor=colors.HexColor('#34495e'),
            spaceAfter=12,
            spaceBefore=12,
            fontName='Helvetica-Bold'
        )
        
        normal_style = styles['Normal']
        
        # Header - School Name
        school_name = Paragraph("<b>SCHOOL FEE BILL</b>", title_style)
        elements.append(school_name)
        elements.append(Spacer(1, 0.2*inch))
        
        # Bill Information Section
        bill_info_data = [
            ['Bill Number:', self.bill.bill_number, 'Date Generated:', self.bill.generated_date.strftime('%B %d, %Y')],
            ['Academic Year:', self.bill.billing_template.academic_year, 'Due Date:', self.bill.due_date.strftime('%B %d, %Y')],
            ['Class:', self.bill.billing_template.class_name, 'Term:', self.bill.billing_template.get_term_display()],
        ]
        
        bill_info_table = Table(bill_info_data, colWidths=[1.5*inch, 2*inch, 1.5*inch, 2*inch])
        bill_info_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#ecf0f1')),
            ('BACKGROUND', (2, 0), (2, -1), colors.HexColor('#ecf0f1')),
            ('TEXTCOLOR', (0, 0), (-1, -1), colors.HexColor('#2c3e50')),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTNAME', (2, 0), (2, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
            ('TOPPADDING', (0, 0), (-1, -1), 8),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ]))
        
        elements.append(bill_info_table)
        elements.append(Spacer(1, 0.3*inch))
        
        # Student Information
        student_heading = Paragraph("<b>Student Information</b>", heading_style)
        elements.append(student_heading)
        
        student_data = [
            ['Student Name:', f"{self.bill.first_name} {self.bill.last_name}"],
            ['Student ID:', str(self.bill.student.id)],
            ['Email:', self.bill.student.email],
            ['Current Class:', self.bill.student.class_name],
        ]
        
        student_table = Table(student_data, colWidths=[2*inch, 5*inch])
        student_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#ecf0f1')),
            ('TEXTCOLOR', (0, 0), (-1, -1), colors.HexColor('#2c3e50')),
            ('ALIGN', (0, 0), (0, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
            ('TOPPADDING', (0, 0), (-1, -1), 8),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ]))
        
        elements.append(student_table)
        elements.append(Spacer(1, 0.3*inch))
        
        # Billing Items
        items_heading = Paragraph("<b>Fee Breakdown</b>", heading_style)
        elements.append(items_heading)
        
        # Table headers
        items_data = [['#', 'Item Name', 'Category', 'Amount (GHS)']]
        
        # Add billing items from template
        billing_items = self.bill.billing_template.billing_items.all()
        for idx, item in enumerate(billing_items, 1):
            items_data.append([
                str(idx),
                item.item_name,
                item.category,
                f"{item.amount:,.2f}"
            ])
        
        # Add custom charges if any
        custom_charges = self.bill.custom_charges.all()
        for idx, charge in enumerate(custom_charges, len(billing_items) + 1):
            items_data.append([
                str(idx),
                f"{charge.charge_name} (Custom)",
                charge.description or 'Custom Charge',
                f"{charge.amount:,.2f}"
            ])
        
        items_table = Table(items_data, colWidths=[0.5*inch, 3*inch, 2*inch, 1.5*inch])
        items_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#3498db')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('ALIGN', (3, 0), (3, -1), 'RIGHT'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 11),
            ('FONTSIZE', (0, 1), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
            ('TOPPADDING', (0, 0), (-1, -1), 8),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8f9fa')]),
        ]))
        
        elements.append(items_table)
        elements.append(Spacer(1, 0.3*inch))
        
        # Financial Summary
        summary_heading = Paragraph("<b>Payment Summary</b>", heading_style)
        elements.append(summary_heading)
        
        # Calculate current bill balance
        current_bill_balance = self.bill.total_amount_due - self.bill.total_paid
        
        summary_data = [
            ['Previous Arrears:', f"GHS {self.bill.previous_arrears:,.2f}"],
            ['Current Term Fees:', f"GHS {self.bill.total_amount_due:,.2f}"],
        ]
        
        # Add discount if applicable
        if self.bill.discount_amount > 0:
            summary_data.append(['Discount Applied:', f"- GHS {self.bill.discount_amount:,.2f}"])
            if self.bill.discount_reason:
                summary_data.append(['Discount Reason:', self.bill.discount_reason])
        
        summary_data.extend([
            ['Amount Paid:', f"GHS {self.bill.total_paid:,.2f}"],
            ['Current Bill Balance:', f"GHS {current_bill_balance:,.2f}"],
            ['TOTAL BALANCE DUE:', f"GHS {self.bill.balance_due:,.2f}"],
        ])
        
        summary_table = Table(summary_data, colWidths=[4*inch, 3*inch])
        summary_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (0, -2), colors.HexColor('#ecf0f1')),
            ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#e74c3c')),
            ('TEXTCOLOR', (0, 0), (-1, -2), colors.HexColor('#2c3e50')),
            ('TEXTCOLOR', (0, -1), (-1, -1), colors.whitesmoke),
            ('ALIGN', (0, 0), (0, -1), 'LEFT'),
            ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -2), 10),
            ('FONTSIZE', (0, -1), (-1, -1), 12),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
            ('TOPPADDING', (0, 0), (-1, -1), 10),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ]))
        
        elements.append(summary_table)
        elements.append(Spacer(1, 0.3*inch))
        
        # Payment Status
        status_color = {
            'paid': colors.HexColor('#27ae60'),
            'partial': colors.HexColor('#f39c12'),
            'pending': colors.HexColor('#e74c3c'),
            'overdue': colors.HexColor('#c0392b')
        }.get(self.bill.payment_status, colors.grey)
        
        status_text = f"<b>Payment Status: <font color='{status_color.hexval()}'>{self.bill.get_payment_status_display().upper()}</font></b>"
        status_para = Paragraph(status_text, normal_style)
        elements.append(status_para)
        elements.append(Spacer(1, 0.2*inch))
        
        # Payment History if any
        payment_receipts = self.bill.payment_receipts.all()
        if payment_receipts:
            payments_heading = Paragraph("<b>Payment History</b>", heading_style)
            elements.append(payments_heading)
            
            payments_data = [['Receipt #', 'Date', 'Method', 'Amount (GHS)']]
            
            for receipt in payment_receipts:
                payments_data.append([
                    receipt.receipt_number,
                    receipt.payment_date.strftime('%b %d, %Y'),
                    receipt.get_payment_method_display(),
                    f"{receipt.amount_paid:,.2f}"
                ])
            
            payments_table = Table(payments_data, colWidths=[2*inch, 1.5*inch, 1.5*inch, 2*inch])
            payments_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#27ae60')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('ALIGN', (3, 0), (3, -1), 'RIGHT'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 11),
                ('FONTSIZE', (0, 1), (-1, -1), 9),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8f9fa')]),
            ]))
            
            elements.append(payments_table)
            elements.append(Spacer(1, 0.2*inch))
        
        # Notes if any
        if self.bill.notes:
            notes_heading = Paragraph("<b>Notes:</b>", heading_style)
            elements.append(notes_heading)
            notes_para = Paragraph(self.bill.notes, normal_style)
            elements.append(notes_para)
            elements.append(Spacer(1, 0.2*inch))
        
        # Footer
        elements.append(Spacer(1, 0.3*inch))
        footer_style = ParagraphStyle(
            'Footer',
            parent=styles['Normal'],
            fontSize=9,
            textColor=colors.grey,
            alignment=TA_CENTER
        )
        
        footer_text = f"""
        <br/>
        <b>Important Notes:</b><br/>
        • Please keep this bill for your records<br/>
        • All payments should reference this bill number: {self.bill.bill_number}<br/>
        • For inquiries, contact the school finance department<br/>
        <br/>
        <i>This is a computer-generated document. Generated on {self.bill.generated_date.strftime('%B %d, %Y at %I:%M %p')}</i>
        """
        footer_para = Paragraph(footer_text, footer_style)
        elements.append(footer_para)
        
        # Build PDF with custom canvas for page numbers
        doc.build(elements, canvasmaker=NumberedCanvas)
        
        # Get the value of the BytesIO buffer
        pdf_content = self.buffer.getvalue()
        self.buffer.close()
        
        return pdf_content


def generate_bill_pdf(student_bill):
    """
    Convenience function to generate PDF for a student bill
    Returns the PDF content as bytes
    """
    generator = BillPDFGenerator(student_bill)
    return generator.generate()