# Generated by Django 5.2 on 2025-04-18 15:59

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('booklist', '0002_alter_studentclasshistory_unique_together_and_more'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='booklist',
            name='publish_date',
        ),
        migrations.AddField(
            model_name='booklist',
            name='scheduled_date',
            field=models.DateTimeField(blank=True, help_text='Date when book list will be automatically published', null=True),
        ),
    ]
