# -*- coding: utf-8 -*-
# Generated by Django 1.11 on 2019-03-20 21:39
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('reference_data', '0010_auto_20190319_1518'),
    ]

    operations = [
        migrations.CreateModel(
            name='PrimateAI',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('percentile_25', models.FloatField()),
                ('percentile_75', models.FloatField()),
                ('gene', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='reference_data.GeneInfo')),
            ],
        ),
    ]