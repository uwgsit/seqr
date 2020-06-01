# -*- coding: utf-8 -*-
# Generated by Django 1.11.29 on 2020-06-01 21:56
from __future__ import unicode_literals
from tqdm import tqdm

from django.db import migrations, models


def update_relationship(apps, schema_editor):
    Family = apps.get_model("seqr", "Family")
    Individual = apps.get_model("seqr", "Individual")
    db_alias = schema_editor.connection.alias
    families = Family.objects.using(db_alias).prefetch_related('individual_set').all()
    if families:
        print('Checking {} families'.format(len(families)))
        proband_ids = []
        for family in tqdm(families, unit=' families'):
            affected_individuals = [indiv for indiv in family.individual_set.all() if indiv.affected == 'A']
            if len(affected_individuals) == 1:
                proband_ids.append(affected_individuals[0].id)

        probands = Individual.objects.filter(id__in=proband_ids)
        num_probands = probands.update(proband_relationship='S')
        print('Updated {} probands'.format(num_probands))

        mother_ids = [indiv.mother_id for indiv in probands if indiv.mother_id]
        num_mothers = Individual.objects.filter(id__in=mother_ids).update(proband_relationship='M')
        print('Updated {} mothers'.format(num_mothers))

        father_ids = [indiv.father_id for indiv in probands if indiv.father_id]
        num_fathers = Individual.objects.filter(id__in=father_ids).update(proband_relationship='F')
        print('Updated {} fathers'.format(num_fathers))


class Migration(migrations.Migration):

    dependencies = [
        ('seqr', '0010_auto_20200413_2159'),
    ]

    operations = [
        migrations.AddField(
            model_name='individual',
            name='proband_relationship',
            field=models.CharField(choices=[
                (b'S', b'Self'), (b'M', b'Mother'), (b'F', b'Father'), (b'B', b'Sibling'), (b'C', b'Child'),
                (b'H', b'Maternal Half Sibling'), (b'J', b'Paternal Half Sibling'), (b'G', b'Maternal Grandmother'),
                (b'W', b'Maternal Grandfather'), (b'X', b'Paternal Grandmother'), (b'Y', b'Paternal Grandfather'),
                (b'A', b'Maternal Aunt'), (b'L', b'Maternal Uncle'), (b'E', b'Paternal Aunt'),
                (b'D', b'Paternal Uncle'), (b'N', b'Niece'), (b'P', b'Nephew'), (b'Z', b'Maternal 1st Cousin'),
                (b'K', b'Paternal 1st Cousin'), (b'O', b'Other'), (b'U', b'Unknown'),
            ], max_length=1, null=True),
        ),
        migrations.RunPython(update_relationship, reverse_code=migrations.RunPython.noop),
    ]
