from rest_framework import serializers
from .models import Ticket, TicketLog

class TicketSerializer(serializers.ModelSerializer):
    class Meta:
        model = Ticket
        fields = '__all__'

class TicketLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = TicketLog
        # Specify the fields you want to serialize

        
        fields = '__all__'
        