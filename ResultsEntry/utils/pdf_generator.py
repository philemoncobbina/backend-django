from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, KeepTogether, PageTemplate, BaseDocTemplate, Frame
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT, TA_JUSTIFY
from reportlab.pdfgen import canvas
from django.conf import settings
from django.core.files.base import ContentFile
from io import BytesIO
import os
from datetime import datetime
import math


class WatermarkCanvas(canvas.Canvas):
    """Custom canvas class to add watermark to each page"""
    
    def __init__(self, *args, **kwargs):
        self.status_text = kwargs.pop('status_text', 'DRAFT')
        canvas.Canvas.__init__(self, *args, **kwargs)
        
    def showPage(self):
        """Override showPage to add watermark before showing the page"""
        self.draw_watermark()
        canvas.Canvas.showPage(self)
        
    def save(self):
        """Override save to add watermark to the last page"""
        self.draw_watermark()
        canvas.Canvas.save(self)
        
    def draw_watermark(self):
        """Draw the watermark on the current page"""
        self.saveState()
        
        # Get page dimensions
        page_width, page_height = A4
        
        # Set watermark properties - larger and more visible
        self.setFont("Helvetica-Bold", 120)
        self.setFillColor(colors.Color(0.85, 0.85, 0.85, alpha=0.4))  # More visible gray with transparency
        
        # Calculate center position
        text_width = self.stringWidth(self.status_text, "Helvetica-Bold", 120)
        x = page_width / 2
        y = page_height / 2
        
        # Rotate and draw the watermark text diagonally
        self.translate(x, y)
        self.rotate(45)  # 45-degree diagonal rotation
        self.drawCentredString(0, 0, self.status_text)
        
        self.restoreState()


class ReportCardPDF:
    def __init__(self, result):
        self.result = result
        self.buffer = BytesIO()
        self.styles = getSampleStyleSheet()
        self.setup_custom_styles()
        
        # Determine status text for watermark
        self.status_text = "ORIGINAL" if self.result.status == 'PUBLISHED' else "DRAFT"
        
    def setup_custom_styles(self):
        """Setup professional custom styles for the report card"""
        
        # School title style
        self.styles.add(ParagraphStyle(
            name='SchoolTitle',
            parent=self.styles['Title'],
            fontSize=18,
            spaceAfter=4,
            spaceBefore=6,
            alignment=TA_CENTER,
            textColor=colors.black,
            fontName='Helvetica-Bold'
        ))
        
        # School subtitle/address style
        self.styles.add(ParagraphStyle(
            name='SchoolSubtitle',
            parent=self.styles['Normal'],
            fontSize=10,
            spaceAfter=2,
            alignment=TA_CENTER,
            textColor=colors.black,
            fontName='Helvetica'
        ))
        
        # Report title style
        self.styles.add(ParagraphStyle(
            name='ReportTitle',
            parent=self.styles['Heading1'],
            fontSize=14,
            spaceAfter=20,
            spaceBefore=15,
            alignment=TA_CENTER,
            textColor=colors.black,
            fontName='Helvetica-Bold'
        ))
        
        # Section header style
        self.styles.add(ParagraphStyle(
            name='SectionHeader',
            parent=self.styles['Heading2'],
            fontSize=11,
            spaceAfter=8,
            spaceBefore=16,
            textColor=colors.black,
            fontName='Helvetica-Bold',
            borderWidth=0,
            borderPadding=4,
            backColor=colors.lightgrey,
            alignment=TA_LEFT
        ))
        
        # Status badge style (now smaller since we have watermark)
        self.styles.add(ParagraphStyle(
            name='StatusBadge',
            parent=self.styles['Normal'],
            fontSize=8,
            alignment=TA_RIGHT,
            textColor=colors.grey,
            fontName='Helvetica-Bold'
        ))
        
        # Footer style
        self.styles.add(ParagraphStyle(
            name='FooterText',
            parent=self.styles['Normal'],
            fontSize=8,
            alignment=TA_CENTER,
            textColor=colors.grey,
            fontName='Helvetica'
        ))
        
        # Remarks style
        self.styles.add(ParagraphStyle(
            name='RemarksText',
            parent=self.styles['Normal'],
            fontSize=10,
            alignment=TA_JUSTIFY,
            textColor=colors.black,
            fontName='Helvetica',
            spaceAfter=6
        ))
    
    def generate_pdf(self):
        """Generate the complete PDF report card with diagonal watermark"""
        
        # Create custom document with watermark canvas
        doc = BaseDocTemplate(
            self.buffer,
            pagesize=A4,
            rightMargin=0.75*inch,
            leftMargin=0.75*inch,
            topMargin=0.75*inch,
            bottomMargin=0.75*inch
        )
        
        # Create frame for content
        frame = Frame(
            0.75*inch, 0.75*inch, 
            A4[0] - 1.5*inch, A4[1] - 1.5*inch, 
            leftPadding=0, bottomPadding=0, 
            rightPadding=0, topPadding=0
        )
        
        # Create page template with diagonal watermark
        template = PageTemplate(
            id='diagonal_watermark_template',
            frames=[frame],
            onPage=self._add_diagonal_watermark
        )
        
        doc.addPageTemplates([template])
        
        story = []
        
        # Add status badge (smaller now since we have watermark)
        story.extend(self._build_status_badge())
        
        # Add school header
        story.extend(self._build_header())
        
        # Add student information
        story.extend(self._build_student_info())
        
        # Add academic performance table
        story.extend(self._build_performance_table())
        
        # Add summary and statistics
        story.extend(self._build_summary_section())
        
        # Add attendance information
        story.extend(self._build_attendance_section())
        
        # Add remarks and promotion info
        story.extend(self._build_remarks_section())
        
        # Add footer
        story.extend(self._build_footer())
        
        # Build the PDF
        doc.build(story)
        
        return self.buffer.getvalue()
    
    def _add_diagonal_watermark(self, canvas, doc):
        """Add large diagonal watermark to each page"""
        canvas.saveState()
        
        # Get page dimensions
        page_width, page_height = A4
        
        # Set watermark properties - large diagonal text
        font_size = 100
        canvas.setFont("Helvetica-Bold", font_size)
        
        # Use a more visible but non-intrusive color
        canvas.setFillColor(colors.Color(0.8, 0.8, 0.8, alpha=0.35))
        
        # Calculate the diagonal position to span across the page
        # Position the watermark to run diagonally from bottom-left to top-right area
        x = page_width / 2
        y = page_height / 2
        
        # Apply transformations for diagonal placement
        canvas.translate(x, y)
        canvas.rotate(45)  # 45-degree diagonal rotation
        
        # Draw the watermark text centered
        canvas.drawCentredString(0, 0, self.status_text)
        
        # Optional: Add a second, smaller watermark for better coverage
        canvas.setFont("Helvetica-Bold", 60)
        canvas.setFillColor(colors.Color(0.9, 0.9, 0.9, alpha=0.25))
        
        # Add smaller watermarks in corners for better visual effect
        canvas.drawCentredString(-150, -100, self.status_text)
        canvas.drawCentredString(150, 100, self.status_text)
        
        canvas.restoreState()
    
    def _build_status_badge(self):
        """Build a smaller status badge (since we now have watermark)"""
        elements = []
        
        # Create smaller status paragraph
        status_para = Paragraph(f"[ {self.status_text} ]", self.styles['StatusBadge'])
        elements.append(status_para)
        elements.append(Spacer(1, 0.05*inch))
        
        return elements
    
    def _build_header(self):
        """Build the professional school header section"""
        elements = []
        
        # School name
        school_name = getattr(settings, 'REPORT_CARD_SETTINGS', {}).get('SCHOOL_NAME', 'SCHOOL NAME')
        elements.append(Paragraph(school_name.upper(), self.styles['SchoolTitle']))
        
        # School address and contact info
        school_address = getattr(settings, 'REPORT_CARD_SETTINGS', {}).get('SCHOOL_ADDRESS', '')
        school_phone = getattr(settings, 'REPORT_CARD_SETTINGS', {}).get('SCHOOL_PHONE', '')
        school_email = getattr(settings, 'REPORT_CARD_SETTINGS', {}).get('SCHOOL_EMAIL', '')
        
        if school_address:
            elements.append(Paragraph(school_address, self.styles['SchoolSubtitle']))
        
        # Contact information on one line
        contact_parts = []
        if school_phone:
            contact_parts.append(f"Tel: {school_phone}")
        if school_email:
            contact_parts.append(f"Email: {school_email}")
        
        if contact_parts:
            contact_text = " • ".join(contact_parts)
            elements.append(Paragraph(contact_text, self.styles['SchoolSubtitle']))
        
        # Horizontal line separator
        elements.append(Spacer(1, 0.1*inch))
        line_table = Table([['_' * 80]], colWidths=[6.5*inch])
        line_table.setStyle(TableStyle([
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
        ]))
        elements.append(line_table)
        
        # Report title
        elements.append(Paragraph("STUDENT REPORT CARD", self.styles['ReportTitle']))
        
        return elements
    
    def _build_student_info(self):
        """Build student information section"""
        elements = []
        
        # Student information in a clean table format
        student_data = [
            ['Student Name:', f"{self.result.student.first_name} {self.result.student.last_name}", 
             'Student ID:', str(self.result.student.id)],
            ['Class:', self.result.class_name, 
             'Academic Year:', self.result.academic_year],
            ['Term:', self.result.get_term_display(), 
             'Position:', self.result.position_context if self.result.overall_position else 'N/A'],
        ]
        
        student_table = Table(student_data, colWidths=[1.2*inch, 2*inch, 1.2*inch, 1.6*inch])
        student_table.setStyle(TableStyle([
            # Alignment
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            
            # Fonts
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),  # Labels column 1
            ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),       # Values column 1
            ('FONTNAME', (2, 0), (2, -1), 'Helvetica-Bold'),  # Labels column 2
            ('FONTNAME', (3, 0), (3, -1), 'Helvetica'),       # Values column 2
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            
            # Padding
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('LEFTPADDING', (0, 0), (-1, -1), 2),
            ('RIGHTPADDING', (0, 0), (-1, -1), 8),
            
            # Borders
            ('BOX', (0, 0), (-1, -1), 1, colors.black),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ]))
        
        elements.append(student_table)
        elements.append(Spacer(1, 0.2*inch))
        
        return elements
    
    def _build_performance_table(self):
        """Build academic performance table"""
        elements = []
        
        # Section header
        elements.append(Paragraph("ACADEMIC PERFORMANCE", self.styles['SectionHeader']))
        elements.append(Spacer(1, 0.1*inch))
        
        # Table headers
        headers = ['Subject', 'Class\nScore\n(40)', 'Exam\nScore\n(60)', 'Total\n(100)', 'Grade', 'Position', 'Remarks']
        
        # Table data
        table_data = [headers]
        
        course_results = self.result.course_results.all().order_by('class_course__course__name')
        
        for course_result in course_results:
            row = [
                course_result.class_course.course.name,
                f"{course_result.class_score:.0f}",
                f"{course_result.exam_score:.0f}",
                f"{course_result.total_score:.0f}",
                course_result.grade,
                course_result.position_context,
                course_result.remarks or '-'
            ]
            table_data.append(row)
        
        # Create the performance table
        performance_table = Table(table_data, colWidths=[
            1.8*inch,  # Subject
            0.7*inch,  # Class Score
            0.7*inch,  # Exam Score
            0.6*inch,  # Total
            0.5*inch,  # Grade
            0.8*inch,  # Position
            1.9*inch   # Remarks
        ])
        
        performance_table.setStyle(TableStyle([
            # Header styling
            ('BACKGROUND', (0, 0), (-1, 0), colors.black),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 9),
            
            # Subject column left aligned
            ('ALIGN', (0, 1), (0, -1), 'LEFT'),
            ('ALIGN', (6, 1), (6, -1), 'LEFT'),  # Remarks column left aligned
            
            # Data styling
            ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 1), (-1, -1), 9),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.lightgrey]),
            
            # Grid and borders
            ('BOX', (0, 0), (-1, -1), 1, colors.black),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.black),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            
            # Padding
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ]))
        
        elements.append(performance_table)
        elements.append(Spacer(1, 0.2*inch))
        
        return elements
    
    def _build_summary_section(self):
        """Build summary statistics section"""
        elements = []
        
        elements.append(Paragraph("PERFORMANCE SUMMARY", self.styles['SectionHeader']))
        elements.append(Spacer(1, 0.1*inch))
        
        summary_data = [
            ['Total Score:', f"{self.result.total_score:.0f}", 
             'Class Position:', self.result.position_context],
            ['Average Score:', f"{self.result.average_score:.1f}%", 
             'Total Students:', str(self.result.total_students_in_class)],
            ['Subjects Offered:', str(len(self.result.course_results.all())), 
             'Term:', self.result.get_term_display()],
        ]
        
        summary_table = Table(summary_data, colWidths=[1.5*inch, 1.5*inch, 1.5*inch, 1.5*inch])
        summary_table.setStyle(TableStyle([
            # Alignment
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            
            # Fonts
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),  # Labels column 1
            ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),       # Values column 1
            ('FONTNAME', (2, 0), (2, -1), 'Helvetica-Bold'),  # Labels column 2
            ('FONTNAME', (3, 0), (3, -1), 'Helvetica'),       # Values column 2
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            
            # Padding and borders
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 8),
            ('BOX', (0, 0), (-1, -1), 1, colors.black),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ]))
        
        elements.append(summary_table)
        elements.append(Spacer(1, 0.2*inch))
        
        return elements
    
    def _build_attendance_section(self):
        """Build attendance information section"""
        elements = []
        
        if self.result.days_present > 0 or self.result.days_absent > 0:
            elements.append(Paragraph("ATTENDANCE RECORD", self.styles['SectionHeader']))
            elements.append(Spacer(1, 0.1*inch))
            
            attendance_data = [
                ['Days Present:', str(self.result.days_present), 
                 'Days Absent:', str(self.result.days_absent)],
                ['Total School Days:', str(self.result.total_days), 
                 'Attendance Rate:', f"{self.result.attendance_percentage:.1f}%"],
            ]
            
            attendance_table = Table(attendance_data, colWidths=[1.5*inch, 1.5*inch, 1.5*inch, 1.5*inch])
            attendance_table.setStyle(TableStyle([
                # Alignment
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                
                # Fonts
                ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
                ('FONTNAME', (2, 0), (2, -1), 'Helvetica-Bold'),
                ('FONTNAME', (3, 0), (3, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                
                # Padding and borders
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('LEFTPADDING', (0, 0), (-1, -1), 4),
                ('RIGHTPADDING', (0, 0), (-1, -1), 8),
                ('BOX', (0, 0), (-1, -1), 1, colors.black),
                ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.grey),
            ]))
            
            elements.append(attendance_table)
            elements.append(Spacer(1, 0.2*inch))
        
        return elements
    
    def _build_remarks_section(self):
        """Build remarks and promotion section"""
        elements = []
        
        # Class teacher remarks
        if self.result.class_teacher_remarks:
            elements.append(Paragraph("CLASS TEACHER'S REMARKS", self.styles['SectionHeader']))
            elements.append(Spacer(1, 0.05*inch))
            
            # Create remarks box
            remarks_data = [[self.result.class_teacher_remarks]]
            remarks_table = Table(remarks_data, colWidths=[6*inch])
            remarks_table.setStyle(TableStyle([
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('TOPPADDING', (0, 0), (-1, -1), 8),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
                ('LEFTPADDING', (0, 0), (-1, -1), 8),
                ('RIGHTPADDING', (0, 0), (-1, -1), 8),
                ('BOX', (0, 0), (-1, -1), 1, colors.black),
            ]))
            
            elements.append(remarks_table)
            elements.append(Spacer(1, 0.15*inch))
        
        # Promotion and next term information
        promotion_info = []
        if self.result.promoted_to:
            promotion_info.append(['Promoted to:', self.result.promoted_to])
        
        if self.result.next_term_begins:
            promotion_info.append(['Next Term Begins:', self.result.next_term_begins.strftime('%B %d, %Y')])
        
        if promotion_info:
            promotion_table = Table(promotion_info, colWidths=[1.5*inch, 4.5*inch])
            promotion_table.setStyle(TableStyle([
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ('LEFTPADDING', (0, 0), (-1, -1), 4),
                ('RIGHTPADDING', (0, 0), (-1, -1), 4),
            ]))
            
            elements.append(promotion_table)
            elements.append(Spacer(1, 0.2*inch))
        
        return elements
    
    def _build_footer(self):
        """Build professional footer section"""
        elements = []
        
        elements.append(Spacer(1, 0.3*inch))
        
        # Signature section
        signature_data = [
            ['', '', ''],  # Empty row for spacing
            ['_' * 20, '_' * 20, '_' * 20],
            ['Class Teacher', 'Principal/Head Teacher', 'Date'],
            ['Signature & Date', 'Signature & Date', ''],
        ]
        
        signature_table = Table(signature_data, colWidths=[2*inch, 2*inch, 2*inch])
        signature_table.setStyle(TableStyle([
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 2), (-1, 2), 'Helvetica-Bold'),
            ('FONTNAME', (0, 3), (-1, 3), 'Helvetica'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('TOPPADDING', (0, 2), (-1, 2), 8),
            ('TOPPADDING', (0, 3), (-1, 3), 2),
            ('TEXTCOLOR', (0, 3), (-1, 3), colors.grey),
        ]))
        
        elements.append(signature_table)
        
        # Generation timestamp
        elements.append(Spacer(1, 0.3*inch))
        generation_time = datetime.now().strftime('%B %d, %Y at %I:%M %p')
        elements.append(Paragraph(f"Generated on {generation_time}", self.styles['FooterText']))
        
        return elements


def generate_report_card_pdf(result):
    """
    Generate a professional PDF report card for a given result with diagonal watermark status
    Returns the PDF file content as bytes
    """
    try:
        pdf_generator = ReportCardPDF(result)
        pdf_content = pdf_generator.generate_pdf()
        return pdf_content
    except Exception as e:
        # Log the error for debugging
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Error generating PDF for result {result.id}: {str(e)}")
        raise