from collections import defaultdict
from elasticsearch_dsl import Index
import json
import logging
import requests
import urllib3

from datetime import datetime, timedelta
from dateutil import relativedelta as rdelta
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.postgres.aggregates import ArrayAgg
from django.db.models import prefetch_related_objects, Q, Prefetch, Max
from django.http.response import HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from requests.exceptions import ConnectionError

from seqr.utils.elasticsearch.utils import get_es_client
from seqr.utils.file_utils import file_iter
from seqr.utils.gene_utils import get_genes
from seqr.utils.xpos_utils import get_chrom_pos

from matchmaker.matchmaker_utils import get_mme_genes_phenotypes_for_submissions, parse_mme_features, \
    parse_mme_gene_variants, get_mme_metrics
from seqr.views.apis.saved_variant_api import _saved_variant_genes, _add_locus_lists
from seqr.views.utils.export_utils import export_multiple_files
from seqr.views.utils.file_utils import parse_file
from seqr.views.utils.json_utils import create_json_response, _to_camel_case
from seqr.views.utils.orm_to_json_utils import _get_json_for_individuals, get_json_for_saved_variants, \
    get_json_for_variant_functional_data_tag_types, get_json_for_projects, _get_json_for_families, \
    get_json_for_locus_lists, _get_json_for_models, get_json_for_matchmaker_submissions, \
    get_json_for_saved_variants_with_tags

from matchmaker.models import MatchmakerSubmission
from seqr.models import Project, Family, VariantTag, VariantTagType, Sample, SavedVariant, Individual, ProjectCategory, \
    LocusList
from reference_data.models import Omim, HumanPhenotypeOntology

from settings import ELASTICSEARCH_SERVER, KIBANA_SERVER, API_LOGIN_REQUIRED_URL, AIRTABLE_API_KEY, AIRTABLE_URL

logger = logging.getLogger(__name__)

HET = 'Heterozygous'
HOM_ALT = 'Homozygous'


@staff_member_required(login_url=API_LOGIN_REQUIRED_URL)
def elasticsearch_status(request):
    client = get_es_client()

    disk_fields = ['node', 'disk.avail', 'disk.used', 'disk.percent']
    disk_status = [{
        _to_camel_case(field.replace('.', '_')): disk[field] for field in disk_fields
    } for disk in client.cat.allocation(format="json", h=','.join(disk_fields))]

    index_fields = ['index', 'docs.count', 'store.size', 'creation.date.string']
    indices = [{
        _to_camel_case(field.replace('.', '_')): index[field] for field in index_fields
    } for index in client.cat.indices(format="json", h=','.join(index_fields))
        if all(not index['index'].startswith(omit_prefix) for omit_prefix in ['.', 'index_operations_log'])]

    aliases = defaultdict(list)
    for alias in client.cat.aliases(format="json", h='alias,index'):
        aliases[alias['alias']].append(alias['index'])

    mappings = Index('_all', using=client).get_mapping(doc_type='variant,structural_variant')

    active_samples = Sample.objects.filter(is_active=True).select_related('individual__family__project')

    seqr_index_projects = defaultdict(lambda: defaultdict(set))
    es_projects = set()
    for sample in active_samples:
        for index_name in sample.elasticsearch_index.split(','):
            project = sample.individual.family.project
            es_projects.add(project)
            if index_name in aliases:
                for aliased_index_name in aliases[index_name]:
                    seqr_index_projects[aliased_index_name][project].add(sample.individual.guid)
            else:
                seqr_index_projects[index_name.rstrip('*')][project].add(sample.individual.guid)

    for index in indices:
        index_name = index['index']
        index_mappings = mappings[index_name]['mappings']
        doc_type = 'variant' if 'variant' in index_mappings else 'structural_variant'
        index.update(index_mappings[doc_type].get('_meta', {}))
        index['docType'] = doc_type

        projects_for_index = []
        for index_prefix in seqr_index_projects.keys():
            if index_name.startswith(index_prefix):
                projects_for_index += seqr_index_projects.pop(index_prefix).keys()
        index['projects'] = [{'projectGuid': project.guid, 'projectName': project.name} for project in projects_for_index]

    errors = [u'{} does not exist and is used by project(s) {}'.format(
        index, ', '.join([u'{} ({} samples)'.format(p.name, len(indivs)) for p, indivs in project_individuals.items()])
    ) for index, project_individuals in seqr_index_projects.items() if project_individuals]

    return create_json_response({
        'indices': indices,
        'diskStats': disk_status,
        'elasticsearchHost': ELASTICSEARCH_SERVER,
        'errors': errors,
    })


@staff_member_required(login_url=API_LOGIN_REQUIRED_URL)
def mme_details(request):
    submissions = MatchmakerSubmission.objects.filter(deleted_date__isnull=True)

    hpo_terms_by_id, genes_by_id, gene_symbols_to_ids = get_mme_genes_phenotypes_for_submissions(submissions)

    submission_json = get_json_for_matchmaker_submissions(
        submissions, additional_model_fields=['label'], all_parent_guids=True)
    submissions_by_guid = {s['submissionGuid']: s for s in submission_json}

    for submission in submissions:
        gene_variants = parse_mme_gene_variants(submission.genomic_features, gene_symbols_to_ids)
        submissions_by_guid[submission.guid].update({
            'phenotypes': parse_mme_features(submission.features, hpo_terms_by_id),
            'geneVariants': gene_variants,
            'geneSymbols': ','.join({genes_by_id.get(gv['geneId'], {}).get('geneSymbol') for gv in gene_variants})
        })

    return create_json_response({
        'metrics': get_mme_metrics(),
        'submissions': submissions_by_guid.values(),
        'genesById': genes_by_id,
    })


@staff_member_required(login_url=API_LOGIN_REQUIRED_URL)
def seqr_stats(request):

    families_count = Family.objects.only('family_id').distinct('family_id').count()
    individuals_count = Individual.objects.only('individual_id').distinct('individual_id').count()

    sample_counts = defaultdict(set)
    for sample in Sample.objects.filter(is_active=True).only('sample_id', 'sample_type'):
        sample_counts[sample.sample_type].add(sample.sample_id)

    for sample_type, sample_ids_set in sample_counts.items():
        sample_counts[sample_type] = len(sample_ids_set)

    return create_json_response({
        'familyCount': families_count,
        'individualCount': individuals_count,
        'sampleCountByType': sample_counts,
    })


SUBJECT_TABLE_COLUMNS = [
    'entity:subject_id', 'subject_id', 'prior_testing', 'project_id', 'pmid_id', 'dbgap_submission', 'dbgap_study_id',
    'dbgap_subject_id', 'multiple_datasets', 'sex', 'ancestry', 'ancestry_detail', 'age_at_last_observation',
    'phenotype_group', 'disease_id', 'disease_description', 'affected_status', 'onset_category', 'age_of_onset',
    'hpo_present', 'hpo_absent', 'phenotype_description', 'solve_state',
]
SAMPLE_TABLE_COLUMNS = [
    'entity:sample_id', 'subject_id', 'sample_id', 'dbgap_sample_id', 'sample_source', 'sample_provider', 'data_type',
    'date_data_generation'
]
FAMILY_TABLE_COLUMNS = [
    'entity:family_id', 'subject_id', 'family_id', 'paternal_id', 'maternal_id', 'twin_id', 'family_relationship',
    'consanguinity', 'consanguinity_detail', 'pedigree_image', 'pedigree_detail', 'family_history', 'family_onset',
]
DISCOVERY_TABLE_CORE_COLUMNS = ['entity:discovery_id', 'subject_id', 'sample_id']
DISCOVERY_TABLE_VARIANT_COLUMNS = [
    'Gene', 'Gene_Class', 'inheritance_description', 'Zygosity', 'Chrom', 'Pos', 'Ref',
    'Alt', 'hgvsc', 'hgvsp', 'Transcript', 'sv_name', 'sv_type', 'significance',
]

PHENOTYPE_PROJECT_CATEGORIES = [
    'Muscle', 'Eye', 'Renal', 'Neuromuscular', 'IBD', 'Epilepsy', 'Orphan', 'Hematologic',
    'Disorders of Sex Development', 'Delayed Puberty', 'Neurodevelopmental', 'Stillbirth', 'ROHHAD', 'Microtia',
    'Diabetes', 'Mitochondrial', 'Cardiovascular',
]

ANCESTRY_MAP = {
  'AFR': 'Black or African American',
  'AMR': 'Hispanic or Latino',
  'ASJ': 'White',
  'EAS': 'Asian',
  'FIN': 'White',
  'MDE': 'Other',
  'NFE': 'White',
  'OTH': 'Other',
  'SAS': 'Asian',
}
ANCESTRY_DETAIL_MAP = {
  'ASJ': 'Ashkenazi Jewish',
  'EAS': 'East Asian',
  'FIN': 'Finnish',
  'MDE': 'Middle Eastern',
  'SAS': 'South Asian',
}

INHERITANCE_MODE_MAP = {
    'X-linked': 'X - linked',
    'AR-homozygote': 'Autosomal recessive (homozygous)',
    'AR-comphet': 'Autosomal recessive (compound heterozygous)',
    'de novo': 'de novo',
    'AD': 'Autosomal dominant',
}

SV_TYPE_MAP = {
    'DUP': 'Duplication',
    'DEL': 'Deletion',
}

MULTIPLE_DATASET_PRODUCTS = {
    'G4L WES + Array v1',
    'G4L WES + Array v2',
    'Standard Exome Plus GWAS Supplement Array',
    'Standard Germline Exome v5 Plus GSA Array',
    'Standard Germline Exome v5 Plus GWAS Supplement Array',
    'Standard Germline Exome v6 Plus GSA Array',
}


@staff_member_required(login_url=API_LOGIN_REQUIRED_URL)
def anvil_export(request, project_guid):
    project = Project.objects.get(guid=project_guid)

    individual_samples = _get_loaded_before_date_project_individual_samples(
        project, request.GET.get('loadedBefore'),
    )

    subject_rows, sample_rows, family_rows, discovery_rows, max_saved_variants = _parse_anvil_metadata(
        project, individual_samples, _get_saved_known_gene_variants_by_family)

    variant_columns = []
    for i in range(max_saved_variants):
        variant_columns += ['{}-{}'.format(k, i + 1) for k in DISCOVERY_TABLE_VARIANT_COLUMNS]

    return export_multiple_files([
        [u'{}_PI_Subject'.format(project.name), SUBJECT_TABLE_COLUMNS, subject_rows],
        [u'{}_PI_Sample'.format(project.name), SAMPLE_TABLE_COLUMNS, sample_rows],
        [u'{}_PI_Family'.format(project.name), FAMILY_TABLE_COLUMNS, family_rows],
        [u'{}_PI_Discovery'.format(project.name), DISCOVERY_TABLE_CORE_COLUMNS + variant_columns, discovery_rows],
    ], u'{}_AnVIL_Metadata'.format(project.name), add_header_prefix=True, file_format='tsv', blank_value='-')


@staff_member_required(login_url=API_LOGIN_REQUIRED_URL)
def sample_metadata_export(request, project_guid):
    project = Project.objects.get(guid=project_guid)

    mme_family_guids = {family.guid for family in _get_has_mme_submission_families(project)}

    individual_samples = _get_loaded_before_date_project_individual_samples(
        project, request.GET.get('loadedBefore') or datetime.now().strftime('%Y-%m-%d'))

    subject_rows, sample_rows, family_rows, discovery_rows, _ = _parse_anvil_metadata(
        project, individual_samples, _get_parsed_saved_discovery_variants_by_family)

    rows_by_subject_id = {row['subject_id']: row for row in subject_rows}
    for rows in [sample_rows, family_rows, discovery_rows]:
        for row in rows:
            rows_by_subject_id[row['subject_id']].update(row)

    rows = rows_by_subject_id.values()
    all_features = set()
    for row in rows:
        row['MME'] = 'Y' if row['family_guid'] in mme_family_guids else 'N'
        if row['ancestry_detail']:
            row['ancestry'] = row['ancestry_detail']
        all_features.update(row['hpo_present'].split('|'))
        all_features.update(row['hpo_absent'].split('|'))

    hpo_name_map = {hpo.hpo_id: hpo.name for hpo in HumanPhenotypeOntology.objects.filter(hpo_id__in=all_features)}
    for row in rows:
        for hpo_key in ['hpo_present', 'hpo_absent']:
            if row[hpo_key]:
                row[hpo_key] = '|'.join(map(
                    lambda feature_id: '{} ({})'.format(feature_id, hpo_name_map.get(feature_id, '')),
                    row[hpo_key].split('|')))

    return create_json_response({'rows': rows})


def _parse_anvil_metadata(project, individual_samples, get_saved_variants_by_family):
    samples_by_family = defaultdict(list)
    individual_id_map = {}
    sample_ids = set()
    for individual, sample in individual_samples.items():
        samples_by_family[individual.family].append(sample)
        individual_id_map[individual.id] = individual.individual_id
        sample_ids.add(sample.sample_id)

    family_individual_affected_guids = {}
    for family, family_samples in samples_by_family.items():
        family_individual_affected_guids[family.guid] = (
            {s.individual.guid for s in family_samples if s.individual.affected == Individual.AFFECTED_STATUS_AFFECTED},
            {s.individual.guid for s in family_samples if s.individual.affected == Individual.AFFECTED_STATUS_UNAFFECTED},
        )

    sample_airtable_metadata = _get_sample_airtable_metadata(list(sample_ids))

    saved_variants_by_family = get_saved_variants_by_family(samples_by_family.keys())
    compound_het_gene_id_by_family = {}
    gene_ids = set()
    max_saved_variants = 1
    for family_guid, saved_variants in saved_variants_by_family.items():
        max_saved_variants = max(max_saved_variants, len(saved_variants))
        potential_com_het_gene_variants = defaultdict(list)
        for variant in saved_variants:
            variant['main_transcript'] = _get_variant_main_transcript(variant)
            if variant['main_transcript']:
                gene_ids.add(variant['main_transcript']['geneId'])

            affected_individual_guids, unaffected_individual_guids = family_individual_affected_guids[family_guid]
            inheritance_models, potential_compound_het_gene_ids = _get_inheritance_models(
                variant, affected_individual_guids, unaffected_individual_guids)
            variant['inheritance_models'] = inheritance_models
            for gene_id in potential_compound_het_gene_ids:
                potential_com_het_gene_variants[gene_id].append(variant)
        for gene_id, comp_het_variants in potential_com_het_gene_variants.items():
            if len(comp_het_variants) > 1:
                main_gene_ids = set()
                for variant in comp_het_variants:
                    variant['inheritance_models'] = {'AR-comphet'}
                    main_gene_ids.add(variant['main_transcript']['geneId'])
                if len(main_gene_ids) > 1:
                    # This occurs in compound hets where some hits have a primary transcripts in different genes
                    for gene_id in main_gene_ids:
                        if all(gene_id in variant['transcripts'] for variant in comp_het_variants):
                            compound_het_gene_id_by_family[family_guid] = gene_id
                            gene_ids.add(gene_id)

    genes_by_id = get_genes(gene_ids)

    mim_numbers = set()
    for family in samples_by_family.keys():
        if family.post_discovery_omim_number:
            mim_numbers.update(family.post_discovery_omim_number.split(','))
    mim_decription_map = {
        str(o.phenotype_mim_number): o.phenotype_description
        for o in Omim.objects.filter(phenotype_mim_number__in=mim_numbers)
    }

    project_details = {
        'project_id': project.name,
        'project_guid': project.guid,
        'phenotype_group': '|'.join([
            category.name for category in project.projectcategory_set.filter(name__in=PHENOTYPE_PROJECT_CATEGORIES)
        ]),
    }

    subject_rows = []
    sample_rows = []
    family_rows = []
    discovery_rows = []
    for family, family_samples in samples_by_family.items():
        saved_variants = saved_variants_by_family[family.guid]

        family_subject_row = {
            'family_guid': family.guid,
            'pmid_id': family.pubmed_ids[0].replace('PMID:', '').strip() if family.pubmed_ids else '',
            'phenotype_description': (family.coded_phenotype or '').replace(',', ';').replace('\t', ' '),
            'num_saved_variants': len(saved_variants),
        }
        family_subject_row.update(project_details)
        if family.post_discovery_omim_number:
            mim_numbers = family.post_discovery_omim_number.split(',')
            family_subject_row.update({
                'disease_id': ';'.join(['OMIM:{}'.format(mim_number) for mim_number in mim_numbers]),
                'disease_description': ';'.join([
                    mim_decription_map.get(mim_number, '') for mim_number in mim_numbers]).replace(',', ';'),
            })

        affected_individual_guids, _ = family_individual_affected_guids[family.guid]

        parsed_variants = []
        for variant in saved_variants:
            if variant['inheritance_models']:
                inheritance_mode = '|'.join([INHERITANCE_MODE_MAP[model] for model in variant['inheritance_models']])
            else:
                inheritance_mode = 'Unknown / Other'
            parsed_variant = {
                'Gene_Class': 'Known',
                'inheritance_description': inheritance_mode,
            }

            if 'discovery_tag_names' in variant:
                is_novel = 'Y' if any('Novel gene' in name for name in variant['discovery_tag_names']) else 'N'
                parsed_variant['novel_mendelian_gene'] = is_novel
                _set_discovery_phenotype_class(parsed_variant, variant['discovery_tag_names'])
                if any('Tier 1' in name for name in variant['discovery_tag_names']):
                    parsed_variant['Gene_Class'] = 'Tier 1 - Candidate'
                elif any('Tier 2' in name for name in variant['discovery_tag_names']):
                    parsed_variant['Gene_Class'] = 'Tier 2 - Candidate'

            if variant.get('svType'):
                parsed_variant.update({
                    'sv_name': _get_sv_name(variant),
                    'sv_type': SV_TYPE_MAP.get(variant['svType'], variant['svType']),
                })
            else:
                gene_id = compound_het_gene_id_by_family.get(family.guid) or variant['main_transcript']['geneId']
                parsed_variant.update({
                    'Gene': genes_by_id[gene_id]['geneSymbol'],
                    'Chrom': variant['chrom'],
                    'Pos': str(variant['pos']),
                    'Ref': variant['ref'],
                    'Alt': variant['alt'],
                    'hgvsc': (variant['main_transcript'].get('hgvsc') or '').split(':')[-1],
                    'hgvsp': (variant['main_transcript'].get('hgvsp') or '').split(':')[-1],
                    'Transcript': variant['main_transcript']['transcriptId'],
                })
            parsed_variants.append((variant['genotypes'], parsed_variant))

        for sample in family_samples:
            individual = sample.individual
            features_present = [feature['id'] for feature in individual.features or []]
            features_absent = [feature['id'] for feature in individual.absent_features or []]
            onset = individual.onset_age

            airtable_metadata = sample_airtable_metadata.get(sample.sample_id, {})
            sequencing = airtable_metadata.get('SequencingProduct') or set()
            multiple_datasets = len(sequencing) > 1 or (
                    len(sequencing) == 1 and list(sequencing)[0] in MULTIPLE_DATASET_PRODUCTS)
            dbgap_submission = airtable_metadata.get('dbgap_submission') or set()
            has_dbgap_submission = sample.sample_type in dbgap_submission

            solve_state = 'Unsolved'
            if parsed_variants:
                all_tier_2 = all(variant[1]['Gene_Class'] == 'Tier 2 - Candidate' for variant in parsed_variants)
                solve_state = 'Tier 2' if all_tier_2 else 'Tier 1'

            subject_row = {
                'entity:subject_id': individual.individual_id,
                'subject_id': individual.individual_id,
                'sex': Individual.SEX_LOOKUP[individual.sex],
                'ancestry': ANCESTRY_MAP.get(individual.population, ''),
                'ancestry_detail': ANCESTRY_DETAIL_MAP.get(individual.population, ''),
                'affected_status': Individual.AFFECTED_STATUS_LOOKUP[individual.affected],
                'onset_category': Individual.ONSET_AGE_LOOKUP[onset] if onset else 'Unknown',
                'hpo_present': '|'.join(features_present),
                'hpo_absent': '|'.join(features_absent),
                'solve_state': solve_state,
                'multiple_datasets': 'Yes' if multiple_datasets else 'No',
                'dbgap_submission': 'No',
                'relationship_to_proband': Individual.RELATIONSHIP_LOOKUP.get(individual.proband_relationship, ''),
            }
            if has_dbgap_submission:
                subject_row.update({
                    'dbgap_submission': 'Yes',
                    'dbgap_study_id': airtable_metadata.get('dbgap_study_id', ''),
                    'dbgap_subject_id': airtable_metadata.get('dbgap_subject_id', ''),
                })
            subject_row.update(family_subject_row)
            subject_rows.append(subject_row)

            sample_row = {
                'entity:sample_id': individual.individual_id,
                'subject_id': individual.individual_id,
                'sample_id': sample.sample_id,
                'data_type': sample.sample_type,
                'date_data_generation': sample.loaded_date.strftime('%Y-%m-%d'),
                'sample_provider': airtable_metadata.get('CollaboratorName') or '',
            }
            if has_dbgap_submission:
                sample_row['dbgap_sample_id'] = airtable_metadata.get('dbgap_sample_id', '')
            sample_rows.append(sample_row)

            family_row = {
                'entity:family_id': individual.individual_id,
                'subject_id': individual.individual_id,
                'family_id': family.family_id,
                'paternal_id': individual_id_map.get(individual.father_id, ''),
                'maternal_id': individual_id_map.get(individual.mother_id, ''),
            }
            if individual.consanguinity is True:
                family_row['consanguinity'] = 'Present'
            elif individual.consanguinity is False:
                family_row['consanguinity'] = 'None suspected'
            if len(affected_individual_guids) > 1:
                family_row['family_history'] = 'Yes'
            family_rows.append(family_row)

            discovery_row = {
                'entity:discovery_id': individual.individual_id,
                'subject_id': individual.individual_id,
                'sample_id': sample.sample_id,
            }
            for i, (genotypes, parsed_variant) in enumerate(parsed_variants):
                genotype = genotypes.get(individual.guid, {})
                zygosity = _get_genotype_zygosity(genotype)
                if zygosity:
                    variant_discovery_row = {
                        'Zygosity': zygosity,
                    }
                    variant_discovery_row.update(parsed_variant)
                    discovery_row.update({'{}-{}'.format(k, i + 1): v for k, v in variant_discovery_row.items()})
            discovery_rows.append(discovery_row)

    return subject_rows, sample_rows, family_rows, discovery_rows, max_saved_variants


def _get_variant_main_transcript(variant):
    main_transcript_id = variant.get('selectedMainTranscriptId') or variant.get('mainTranscriptId')
    if not main_transcript_id:
        return {}
    for transcripts in variant.get('transcripts', {}).values():
        main_transcript = next((t for t in transcripts if t['transcriptId'] == main_transcript_id), None)
        if main_transcript:
            return main_transcript


def _get_sv_name(variant_json):
    return variant_json.get('svName') or '{svType}:chr{chrom}:{pos}-{end}'.format(**variant_json)


def _get_loaded_before_date_project_individual_samples(project, max_loaded_date):
    if max_loaded_date:
        max_loaded_date = datetime.strptime(max_loaded_date, '%Y-%m-%d')
    else:
        max_loaded_date = datetime.now() - timedelta(days=365)

    loaded_samples = Sample.objects.filter(
        individual__family__project=project,
    ).select_related('individual__family').order_by('-loaded_date')
    if max_loaded_date:
        loaded_samples = loaded_samples.filter(loaded_date__lte=max_loaded_date)
    #  Only return the oldest sample for each individual
    return {sample.individual: sample for sample in loaded_samples}


def _get_saved_known_gene_variants_by_family(families):
    tag_type = VariantTagType.objects.get(name='Known gene for phenotype')

    project_saved_variants = SavedVariant.objects.select_related('family').filter(
        varianttag__variant_tag_type=tag_type,
        family__in=families,
    )

    project_saved_variants_json = get_json_for_saved_variants(project_saved_variants, add_details=True)

    saved_variants_by_family = defaultdict(list)
    for variant in project_saved_variants_json:
        for family_guid in variant['familyGuids']:
            saved_variants_by_family[family_guid].append(variant)

    return saved_variants_by_family


MAX_FILTER_IDS = 500
SAMPLE_ID_FIELDS = ['SeqrCollaboratorSampleID', 'CollaboratorSampleID']
SINGLE_SAMPLE_FIELDS = ['Collaborator', 'dbgap_study_id', 'dbgap_subject_id', 'dbgap_sample_id']
LIST_SAMPLE_FIELDS = ['SequencingProduct', 'dbgap_submission']


def _get_sample_airtable_metadata(sample_ids):
    raw_records = {}
    # Airtable does handle its own pagination, but the query URI has a max length so the filter formula needs to be truncated
    for index in range(0, len(sample_ids), MAX_FILTER_IDS):
        raw_records.update(_fetch_airtable_records(
            'Samples',
            fields=SAMPLE_ID_FIELDS + SINGLE_SAMPLE_FIELDS + LIST_SAMPLE_FIELDS,
            filter_formula='OR({})'.format(','.join([
                "{{CollaboratorSampleID}}='{sample_id}',{{SeqrCollaboratorSampleID}}='{sample_id}'".format(sample_id=sample_id)
                for sample_id in sample_ids[index:index+MAX_FILTER_IDS]]))
        ))
    sample_records = {}
    collaborator_ids = set()
    for record in raw_records.values():
        record_id = next(record[id_field] for id_field in SAMPLE_ID_FIELDS if record.get(id_field))
        if record.get('Collaborator'):
            collaborator = record['Collaborator'][0]
            collaborator_ids.add(collaborator)
            record['Collaborator'] = collaborator

        parsed_record = sample_records.get(record_id, {})
        for field in SINGLE_SAMPLE_FIELDS:
            if field in record:
                if field in parsed_record and parsed_record[field] != record[field]:
                    error = 'Found multiple airtable records for sample {} with mismatched values in field {}'.format(
                        record_id, field)
                    raise Exception(error)
                parsed_record[field] = record[field]
        for field in LIST_SAMPLE_FIELDS:
            if field in record:
                value = parsed_record.get(field, set())
                value.update(record[field])
                parsed_record[field] = value

        sample_records[record_id] = parsed_record

    if collaborator_ids:
        collaborator_map = _fetch_airtable_records(
            'Collaborator', fields=['CollaboratorID'], filter_formula='OR({})'.format(
                ','.join(["RECORD_ID()='{}'".format(collaborator) for collaborator in collaborator_ids])))

        for sample in sample_records.values():
            sample['CollaboratorName'] = collaborator_map.get(sample.get('Collaborator'), {}).get('CollaboratorID')

    return sample_records


def _fetch_airtable_records(record_type, fields=None, filter_formula=None, offset=None, records=None):
    headers = {'Authorization': 'Bearer {}'.format(AIRTABLE_API_KEY)}

    params = {}
    if offset:
        params['offset'] = offset
    if fields:
        params['fields[]'] = fields
    if filter_formula:
        params['filterByFormula'] = filter_formula
    response = requests.get('{}/{}'.format(AIRTABLE_URL, record_type), params=params, headers=headers)
    response.raise_for_status()
    if not records:
        records = {}
    try:
        response_json = response.json()
        records.update({record['id']: record['fields'] for record in response_json['records']})
    except (ValueError, KeyError) as e:
        raise Exception('Unable to retrieve airtable data: {}'.format(e))

    if response_json.get('offset'):
        return _fetch_airtable_records(
            record_type, fields=fields, filter_formula=filter_formula, offset=response_json['offset'], records=records)

    logger.info('Fetched {} {} records from airtable'.format(len(records), record_type))
    return records


# HPO categories are direct children of HP:0000118 "Phenotypic abnormality".
# See http://compbio.charite.de/hpoweb/showterm?id=HP:0000118
HPO_CATEGORY_NAMES = {
    'HP:0000478': 'Eye Defects',
    'HP:0025142': 'Constitutional Symptom',
    'HP:0002664': 'Neoplasm',
    'HP:0000818': 'Endocrine System',
    'HP:0000152': 'Head or Neck',
    'HP:0002715': 'Immune System',
    'HP:0001507': 'Growth',
    'HP:0045027': 'Thoracic Cavity',
    'HP:0001871': 'Blood',
    'HP:0002086': 'Respiratory',
    'HP:0000598': 'Ear Defects',
    'HP:0001939': 'Metabolism/Homeostasis',
    'HP:0003549': 'Connective Tissue',
    'HP:0001608': 'Voice',
    'HP:0000707': 'Nervous System',
    'HP:0000769': 'Breast',
    'HP:0001197': 'Prenatal development or birth',
    'HP:0040064': 'Limbs',
    'HP:0025031': 'Abdomen',
    'HP:0003011': 'Musculature',
    'HP:0001626': 'Cardiovascular System',
    'HP:0000924': 'Skeletal System',
    'HP:0500014': 'Test Result',
    'HP:0001574': 'Integument',
    'HP:0000119': 'Genitourinary System',
    'HP:0025354': 'Cellular Phenotype',
}

DEFAULT_ROW = {
    "t0": None,
    "t0_copy": None,
    "months_since_t0": None,
    "sample_source": "CMG",
    "analysis_complete_status": "complete",
    "expected_inheritance_model": "multiple",
    "actual_inheritance_model": "",
    "n_kindreds": "1",
    "gene_name": "NS",
    "novel_mendelian_gene": "NS",
    "gene_count": "NA",
    "phenotype_class": "New",
    "solved": "N",
    "genome_wide_linkage": "NS",
    "p_value": "NS",
    "n_kindreds_overlapping_sv_similar_phenotype": "NS",
    "n_unrelated_kindreds_with_causal_variants_in_gene": "NS",
    "biochemical_function": "NS",
    "protein_interaction": "NS",
    "expression": "NS",
    "patient_cells": "NS",
    "non_patient_cell_model": "NS",
    "animal_model": "NS",
    "non_human_cell_culture_model": "NS",
    "rescue": "NS",
    "omim_number_initial": "NA",
    "omim_number_post_discovery": "NA",
    "submitted_to_mme": "NS",
    "posted_publicly": "NS",
    "komp_early_release": "NS",
}
DEFAULT_ROW.update({hpo_category: 'N' for hpo_category in [
    "connective_tissue",
    "voice",
    "nervous_system",
    "breast",
    "eye_defects",
    "prenatal_development_or_birth",
    "neoplasm",
    "endocrine_system",
    "head_or_neck",
    "immune_system",
    "growth",
    "limbs",
    "thoracic_cavity",
    "blood",
    "musculature",
    "cardiovascular_system",
    "abdomen",
    "skeletal_system",
    "respiratory",
    "ear_defects",
    "metabolism_homeostasis",
    "genitourinary_system",
    "integument",
]})

ADDITIONAL_KINDREDS_FIELD = "n_unrelated_kindreds_with_causal_variants_in_gene"
OVERLAPPING_KINDREDS_FIELD = "n_kindreds_overlapping_sv_similar_phenotype"
FUNCTIONAL_DATA_FIELD_MAP = {
    "Additional Unrelated Kindreds w/ Causal Variants in Gene": ADDITIONAL_KINDREDS_FIELD,
    "Genome-wide Linkage": "genome_wide_linkage",
    "Bonferroni corrected p-value": "p_value",
    "Kindreds w/ Overlapping SV & Similar Phenotype": OVERLAPPING_KINDREDS_FIELD,
    "Biochemical Function": "biochemical_function",
    "Protein Interaction": "protein_interaction",
    "Expression": "expression",
    "Patient Cells": "patient_cells",
    "Non-patient cells": "non_patient_cell_model",
    "Animal Model": "animal_model",
    "Non-human cell culture model": "non_human_cell_culture_model",
    "Rescue": "rescue",
}
METADATA_FUNCTIONAL_DATA_FIELDS = {
    "genome_wide_linkage",
    "p_value",
    OVERLAPPING_KINDREDS_FIELD,
    ADDITIONAL_KINDREDS_FIELD,
}


@staff_member_required(login_url=API_LOGIN_REQUIRED_URL)
def get_projects_for_category(request, project_category_name):
    category = ProjectCategory.objects.get(name=project_category_name)
    return create_json_response({
        'projectGuids': [p.guid for p in Project.objects.filter(projectcategory=category).only('guid')],
    })


@staff_member_required(login_url=API_LOGIN_REQUIRED_URL)
def discovery_sheet(request, project_guid):
    project = Project.objects.filter(guid=project_guid).prefetch_related(
        Prefetch('family_set', to_attr='families', queryset=Family.objects.prefetch_related('individual_set'))
    ).distinct().first()
    if not project:
        message = 'Invalid project {}'.format(project_guid)
        return create_json_response({'error': message}, status = 400, reason = message)

    rows = []
    errors = []

    loaded_samples_by_family = _get_loaded_samples_by_family(project)
    saved_variants_by_family = _get_project_saved_discovery_variants_by_family(project)
    mme_submission_families = _get_has_mme_submission_families(project)

    if not loaded_samples_by_family:
        errors.append("No data loaded for project: %s" % project)
        logger.info("No data loaded for project: %s" % project)
        return create_json_response({
            'rows': [],
            'errors': errors,
        })

    if "external" in project.name.lower() or "reprocessed" in project.name.lower():
        sequencing_approach = "REAN"
    else:
        sequencing_approach = loaded_samples_by_family.values()[0][-1].sample_type
    initial_row = {
        "project_guid": project.guid,
        "collaborator": project.name,
        "sequencing_approach": sequencing_approach,
    }
    initial_row.update(DEFAULT_ROW)

    now = timezone.now()
    for family in project.families:
        samples = loaded_samples_by_family.get(family.guid)
        if not samples:
            errors.append("No data loaded for family: %s. Skipping..." % family)
            continue
        saved_variants = saved_variants_by_family.get(family.guid)
        submitted_to_mme = family in mme_submission_families

        rows += _generate_rows(initial_row, family, samples, saved_variants, submitted_to_mme, errors, now=now)

    _update_gene_symbols(rows)
    _update_hpo_categories(rows, errors)
    _update_initial_omim_numbers(rows)

    return create_json_response({
        'rows': rows,
        'errors': errors,
    })


@staff_member_required(login_url=API_LOGIN_REQUIRED_URL)
def success_story(request, success_story_types):
    if success_story_types == 'all':
        families = Family.objects.filter(success_story__isnull=False)
    else:
        success_story_types = success_story_types.split(',')
        families = Family.objects.filter(success_story_types__overlap=success_story_types)

    rows = [{
        "project_guid": family.project.guid,
        "family_guid": family.guid,
        "family_id": family.family_id,
        "success_story_types": family.success_story_types,
        "success_story": family.success_story,
        "row_id": family.guid,
    } for family in families]

    return create_json_response({
        'rows': rows,
    })


def _get_loaded_samples_by_family(project):
    loaded_samples = Sample.objects.filter(individual__family__project=project).select_related(
        'individual__family').order_by('loaded_date')

    loaded_samples_by_family = defaultdict(list)
    for sample in loaded_samples:
        family = sample.individual.family
        loaded_samples_by_family[family.guid].append(sample)

    return loaded_samples_by_family


def _get_parsed_saved_discovery_variants_by_family(families):
    return _get_saved_discovery_variants_by_family({'family__in': families}, parse_json=True)


def _get_project_saved_discovery_variants_by_family(project):
    return _get_saved_discovery_variants_by_family({'family__project': project}, parse_json=False)


def _get_saved_discovery_variants_by_family(variant_filter, parse_json=False):
    tag_types = VariantTagType.objects.filter(project__isnull=True, category='CMG Discovery Tags')

    project_saved_variants = SavedVariant.objects.select_related('family').prefetch_related(
        Prefetch('varianttag_set', to_attr='discovery_tags',
                 queryset=VariantTag.objects.filter(variant_tag_type__in=tag_types).select_related('variant_tag_type'),
                 )).prefetch_related('variantfunctionaldata_set').filter(
        varianttag__variant_tag_type__in=tag_types,
        **variant_filter
    )

    if parse_json:
        variant_by_guid = {variant['variantGuid']: variant for variant in
                           get_json_for_saved_variants(project_saved_variants, add_details=True)}

    saved_variants_by_family = defaultdict(list)
    for saved_variant in project_saved_variants:
        parsed_variant = saved_variant
        if parse_json:
            parsed_variant = variant_by_guid[saved_variant.guid]
            parsed_variant['discovery_tag_names'] = {vt.variant_tag_type.name for vt in saved_variant.discovery_tags}
        saved_variants_by_family[saved_variant.family.guid].append(parsed_variant)

    return saved_variants_by_family


def _get_has_mme_submission_families(project):
    return {
        submission.individual.family for submission in MatchmakerSubmission.objects.filter(
            individual__family__project=project,
        ).select_related('individual__family')
    }


def _generate_rows(initial_row, family, samples, saved_variants, submitted_to_mme, errors, now=timezone.now()):
    row = _get_basic_row(initial_row, family, samples, now)
    if submitted_to_mme:
        row["submitted_to_mme"] = "Y"

    individuals = family.individual_set.all()

    expected_inheritance_models = []
    mim_disorders = []
    row['features'] = set()
    for i in individuals:
        expected_inheritance_models += i.expected_inheritance or []
        mim_disorders += i.disorders or []
        row['features'].update([feature['id'] for feature in i.features or []])

    if len(expected_inheritance_models) == 1:
        row["expected_inheritance_model"] = Individual.INHERITANCE_LOOKUP[expected_inheritance_models[0]]

    if mim_disorders:
        row.update({
            "omim_number_initial": mim_disorders[0],
            "phenotype_class": "KNOWN",
        })

    if family.post_discovery_omim_number:
        row["omim_number_post_discovery"] = family.post_discovery_omim_number

    if not saved_variants:
        return [row]

    for variant in saved_variants:
        if not variant.saved_variant_json:
            errors.append("%s - variant annotation not found" % variant)
            return [row]

        if not variant.saved_variant_json.get('transcripts') and not variant.saved_variant_json.get('svName'):
            errors.append("%s - no gene ids" % variant)
            return [row]

    affected_individual_guids = set()
    unaffected_individual_guids = set()
    for sample in samples:
        if sample.individual.affected == "A":
            affected_individual_guids.add(sample.individual.guid)
        elif sample.individual.affected == "N":
            unaffected_individual_guids.add(sample.individual.guid)

    potential_compound_het_genes = defaultdict(set)
    for variant in saved_variants:
        _update_variant_inheritance(
            variant, affected_individual_guids, unaffected_individual_guids, potential_compound_het_genes)

    gene_ids_to_saved_variants, gene_ids_to_variant_tag_names, gene_ids_to_inheritance = _get_gene_to_variant_info_map(
        saved_variants, potential_compound_het_genes)

    if len(gene_ids_to_saved_variants) > 1:
        row["gene_count"] = len(gene_ids_to_saved_variants)

    rows = []
    for gene_id, variants in gene_ids_to_saved_variants.items():
        rows.append(_get_gene_row(
            dict(row), gene_id, gene_ids_to_inheritance[gene_id], gene_ids_to_variant_tag_names[gene_id], variants))
    return rows


def _get_basic_row(initial_row, family, samples, now):
    row = {
        "family_guid": family.guid,
        "family_id": family.family_id,
        "extras_pedigree_url": family.pedigree_image.url if family.pedigree_image else "",
        "coded_phenotype": family.coded_phenotype or "",
        "pubmed_ids": '; '.join(family.pubmed_ids),
        "analysis_summary": (family.analysis_summary or '').strip('" \n'),
        "row_id": family.guid,
        "num_individuals_sequenced": len({sample.individual for sample in samples})
    }
    row.update(initial_row)

    t0 = samples[0].loaded_date
    t0_diff = rdelta.relativedelta(now, t0)
    t0_months_since_t0 = t0_diff.years * 12 + t0_diff.months
    row.update({
        "t0": t0,
        "t0_copy": t0,
        "months_since_t0": t0_months_since_t0,
    })
    if t0_months_since_t0 < 12:
        row['analysis_complete_status'] = "first_pass_in_progress"
    return row


def _get_inheritance_models(variant_json, affected_individual_guids, unaffected_individual_guids):
    inheritance_models = set()

    affected_indivs_with_hom_alt_variants = set()
    affected_indivs_with_het_variants = set()
    unaffected_indivs_with_hom_alt_variants = set()
    unaffected_indivs_with_het_variants = set()
    is_x_linked = False

    genotypes = variant_json.get('genotypes')
    if genotypes:
        chrom = variant_json['chrom']
        is_x_linked = "X" in chrom
        _get_variant_genotypes(
            genotypes, affected_individual_guids, unaffected_individual_guids, affected_indivs_with_hom_alt_variants,
            affected_indivs_with_het_variants, unaffected_indivs_with_hom_alt_variants, unaffected_indivs_with_het_variants)

    # AR-homozygote, AR-comphet, AR, AD, de novo, X-linked, UPD, other, multiple
    if not unaffected_indivs_with_hom_alt_variants and affected_indivs_with_hom_alt_variants:
        if is_x_linked:
            inheritance_models.add("X-linked")
        else:
            inheritance_models.add("AR-homozygote")

    if not unaffected_indivs_with_hom_alt_variants and not unaffected_indivs_with_het_variants and affected_indivs_with_het_variants:
        if unaffected_individual_guids:
            inheritance_models.add("de novo")
        else:
            inheritance_models.add("AD")

    potential_compound_het_gene_ids = set()
    if not unaffected_indivs_with_hom_alt_variants and (
        len(unaffected_individual_guids) < 2 or unaffected_indivs_with_het_variants
    ) and affected_indivs_with_het_variants and not affected_indivs_with_hom_alt_variants and 'transcripts' in variant_json:
        potential_compound_het_gene_ids.update(variant_json['transcripts'].keys())

    return inheritance_models, potential_compound_het_gene_ids


def _update_variant_inheritance(variant, affected_individual_guids, unaffected_individual_guids, potential_compound_het_genes):
    inheritance_models, potential_compound_het_gene_ids = _get_inheritance_models(
        variant.saved_variant_json, affected_individual_guids, unaffected_individual_guids)
    variant.saved_variant_json['inheritance'] = inheritance_models

    for gene_id in potential_compound_het_gene_ids:
        potential_compound_het_genes[gene_id].add(variant)

    main_transcript_id = variant.selected_main_transcript_id or variant.saved_variant_json.get('mainTranscriptId')
    if main_transcript_id:
        for gene_id, transcripts in variant.saved_variant_json['transcripts'].items():
            if any(t['transcriptId'] == main_transcript_id for t in transcripts):
                variant.saved_variant_json['mainTranscriptGeneId'] = gene_id
                break
    elif len(variant.saved_variant_json.get('transcripts', {})) == 1 and not variant.saved_variant_json['transcripts'].values()[0]:
        variant.saved_variant_json['mainTranscriptGeneId'] = variant.saved_variant_json['transcripts'].keys()[0]


def _get_variant_genotypes(genotypes, affected_individual_guids, unaffected_individual_guids,
                           affected_indivs_with_hom_alt_variants, affected_indivs_with_het_variants,
                           unaffected_indivs_with_hom_alt_variants, unaffected_indivs_with_het_variants):
    for sample_guid, genotype in genotypes.items():
        zygosity = _get_genotype_zygosity(genotype)
        if zygosity == HOM_ALT and sample_guid in affected_individual_guids:
            affected_indivs_with_hom_alt_variants.add(sample_guid)
        elif zygosity == HET and sample_guid in affected_individual_guids:
            affected_indivs_with_het_variants.add(sample_guid)
        elif zygosity == HOM_ALT and sample_guid in unaffected_individual_guids:
            unaffected_indivs_with_hom_alt_variants.add(sample_guid)
        elif zygosity == HET and sample_guid in unaffected_individual_guids:
            unaffected_indivs_with_het_variants.add(sample_guid)


def _get_genotype_zygosity(genotype):
    num_alt = genotype.get('numAlt')
    cn = genotype.get('cn')
    if num_alt == 2 or cn == 0 or cn > 3:
        return HOM_ALT
    if num_alt == 1 or cn == 1 or cn == 3:
        return HET
    return None


def _get_gene_to_variant_info_map(saved_variants, potential_compound_het_genes):
    gene_ids_to_saved_variants = defaultdict(set)
    gene_ids_to_variant_tag_names = defaultdict(set)
    gene_ids_to_inheritance = defaultdict(set)
    # Compound het variants are reported in the gene that they share
    for gene_id, variants in potential_compound_het_genes.items():
        if len(variants) > 1:
            gene_ids_to_inheritance[gene_id].add("AR-comphet")
            # Only include compound hets for one of the genes they are both in
            existing_gene_id = next((
                existing_gene_id for existing_gene_id, existing_variants in gene_ids_to_saved_variants.items()
                if existing_variants == variants), None)
            if existing_gene_id:
                main_gene_ids = {
                    variant.saved_variant_json['mainTranscriptGeneId'] for variant in variants
                }
                if gene_id in main_gene_ids:
                    gene_ids_to_saved_variants[gene_id] = gene_ids_to_saved_variants[existing_gene_id]
                    del gene_ids_to_saved_variants[existing_gene_id]
                    gene_ids_to_variant_tag_names[gene_id] = gene_ids_to_variant_tag_names[existing_gene_id]
                    del gene_ids_to_variant_tag_names[existing_gene_id]
            else:
                for variant in variants:
                    variant.saved_variant_json['inheritance'] = {"AR-comphet"}
                    gene_ids_to_variant_tag_names[gene_id].update(
                        {vt.variant_tag_type.name for vt in variant.discovery_tags})
                gene_ids_to_saved_variants[gene_id].update(variants)

    # Non-compound het variants are reported in the main transcript gene
    for variant in saved_variants:
        if "AR-comphet" not in variant.saved_variant_json['inheritance']:
            gene_id = variant.saved_variant_json.get('mainTranscriptGeneId')
            if not gene_id and variant.saved_variant_json.get('svType'):
                gene_id = _get_sv_name(variant.saved_variant_json)
            gene_ids_to_saved_variants[gene_id].add(variant)
            gene_ids_to_variant_tag_names[gene_id].update({vt.variant_tag_type.name for vt in variant.discovery_tags})
            gene_ids_to_inheritance[gene_id].update(variant.saved_variant_json['inheritance'])

    return gene_ids_to_saved_variants, gene_ids_to_variant_tag_names, gene_ids_to_inheritance


def _get_gene_row(row, gene_id, inheritances, variant_tag_names, variants):
    row["actual_inheritance_model"] = ", ".join(inheritances)

    row["gene_id"] = gene_id
    row["row_id"] += gene_id

    has_tier1 = any(name.startswith("Tier 1") for name in variant_tag_names)
    has_tier2 = any(name.startswith("Tier 2") for name in variant_tag_names)
    has_known_gene_for_phenotype = 'Known gene for phenotype' in variant_tag_names

    row.update({
        "solved": ("TIER 1 GENE" if (has_tier1 or has_known_gene_for_phenotype) else (
            "TIER 2 GENE" if has_tier2 else "N")),
        "komp_early_release": "Y" if 'Share with KOMP' in variant_tag_names else "N",
    })

    if has_tier1 or has_tier2 or has_known_gene_for_phenotype:
        row.update({
            "posted_publicly": "",
            "analysis_complete_status": "complete",
            "novel_mendelian_gene": "Y" if any("Novel gene" in name for name in variant_tag_names) else "N",
        })

    if has_tier1 or has_tier2:
        _set_discovery_details(row, variant_tag_names, variants)
    elif has_known_gene_for_phenotype:
        row["phenotype_class"] = "KNOWN"
        for functional_field in FUNCTIONAL_DATA_FIELD_MAP.values():
            row[functional_field] = "KPG"

    if not row["submitted_to_mme"] == 'Y':
        if has_tier1 or has_tier2:
            row["submitted_to_mme"] = "N" if row['months_since_t0'] > 7 else "TBD"
        elif has_known_gene_for_phenotype:
            row["submitted_to_mme"] = "KPG"

    row["extras_variant_tag_list"] = []
    for variant in variants:
        variant_id = variant.saved_variant_json.get('variantId')
        if not variant_id:
            variant_id = "-".join(map(str, list(get_chrom_pos(variant.xpos_start)) + [variant.ref, variant.alt]))
        row["extras_variant_tag_list"] += [
            (variant_id, gene_id, vt.variant_tag_type.name.lower()) for vt in variant.discovery_tags
        ]
    return row


def _set_discovery_phenotype_class(row, variant_tag_names):
    if any(tag in variant_tag_names for tag in [
        'Tier 1 - Known gene, new phenotype', 'Tier 2 - Known gene, new phenotype',
    ]):
        row["phenotype_class"] = "NEW"
    elif any(tag in variant_tag_names for tag in [
        'Tier 1 - Phenotype expansion', 'Tier 1 - Novel mode of inheritance', 'Tier 2 - Phenotype expansion',
    ]):
        row["phenotype_class"] = "EXPAN"
    elif any(tag in variant_tag_names for tag in [
        'Tier 1 - Phenotype not delineated', 'Tier 2 - Phenotype not delineated'
    ]):
        row["phenotype_class"] = "UE"
    elif 'Known gene for phenotype' in variant_tag_names:
        row["phenotype_class"] = "KNOWN"


def _set_discovery_details(row, variant_tag_names, variants):
    _set_discovery_phenotype_class(row, variant_tag_names)

    # Set defaults
    for functional_field in FUNCTIONAL_DATA_FIELD_MAP.values():
        if functional_field == ADDITIONAL_KINDREDS_FIELD:
            row[functional_field] = "1"
        elif functional_field in METADATA_FUNCTIONAL_DATA_FIELDS:
            row[functional_field] = "NA"
        else:
            row[functional_field] = "N"
    # Set values
    for variant in variants:
        for f in variant.variantfunctionaldata_set.all():
            functional_field = FUNCTIONAL_DATA_FIELD_MAP[f.functional_data_tag]
            if functional_field in METADATA_FUNCTIONAL_DATA_FIELDS:
                value = f.metadata
                if functional_field == ADDITIONAL_KINDREDS_FIELD:
                    value = str(int(value) + 1)
                elif functional_field == OVERLAPPING_KINDREDS_FIELD:
                    existing_val = row[functional_field]
                    if existing_val != 'NA':
                        value = str(max(int(existing_val), int(value)))
                elif row[functional_field] != 'NS':
                    value = '{} {}'.format(row[functional_field], value)
            else:
                value = 'Y'

            row[functional_field] = value


def _update_gene_symbols(rows):
    genes_by_id = get_genes({row['gene_id'] for row in rows if row.get('gene_id')})
    for row in rows:
        if row.get('gene_id'):
            row['gene_name'] = genes_by_id.get(row['gene_id'], {}).get('geneSymbol') or row['gene_id']

        row["extras_variant_tag_list"] = ["{variant_id}  {gene_symbol}  {tag}".format(
            variant_id=variant_id, gene_symbol=genes_by_id.get(gene_id, {}).get('geneSymbol', ''), tag=tag,
        ) for variant_id, gene_id, tag in row.get("extras_variant_tag_list", [])]


def _update_hpo_categories(rows, errors):
    all_features = set()
    for row in rows:
        all_features.update(row['features'])

    hpo_term_to_category = {
        hpo.hpo_id: hpo.category_id for hpo in HumanPhenotypeOntology.objects.filter(hpo_id__in=all_features)
    }

    for row in rows:
        category_not_set_on_some_features = False
        for feature in row.pop('features'):
            category = hpo_term_to_category.get(feature)
            if not category:
                category_not_set_on_some_features = True
                continue

            hpo_category_name = HPO_CATEGORY_NAMES[category]
            key = hpo_category_name.lower().replace(" ", "_").replace("/", "_")
            row[key] = "Y"

        if category_not_set_on_some_features:
            errors.append('HPO category field not set for some HPO terms in {}'.format(row['family_id']))


def _update_initial_omim_numbers(rows):
    omim_numbers = {row['omim_number_initial'] for row in rows if row['omim_number_initial'] and row['omim_number_initial'] != 'NA'}

    omim_number_map = {str(omim.phenotype_mim_number): omim.phenotypic_series_number
                       for omim in Omim.objects.filter(phenotype_mim_number__in=omim_numbers, phenotypic_series_number__isnull=False)}

    for mim_number, phenotypic_series_number in omim_number_map.items():
        logger.info("Will replace OMIM initial # %s with phenotypic series %s" % (mim_number, phenotypic_series_number))

    for row in rows:
        if omim_number_map.get(row['omim_number_initial']):
            row['omim_number_initial'] = omim_number_map[row['omim_number_initial']]


@staff_member_required(login_url=API_LOGIN_REQUIRED_URL)
def saved_variants_page(request, tag):
    gene = request.GET.get('gene')
    tag_type = VariantTagType.objects.get(name=tag, project__isnull=True)
    saved_variant_models = SavedVariant.objects.filter(varianttag__variant_tag_type=tag_type)
    if gene:
        saved_variant_models = saved_variant_models.filter(saved_variant_json__transcripts__has_key=gene)

    if saved_variant_models.count() > 10000 and not gene:
        return create_json_response({'message': 'Select a gene to filter variants'}, status=400)

    prefetch_related_objects(saved_variant_models, 'family__project')
    response_json = get_json_for_saved_variants_with_tags(saved_variant_models, add_details=True, include_missing_variants=True)

    project_models_by_guid = {variant.family.project.guid: variant.family.project for variant in saved_variant_models}
    families = {variant.family for variant in saved_variant_models}
    individuals = Individual.objects.filter(family__in=families)

    saved_variants = response_json['savedVariantsByGuid'].values()
    genes = _saved_variant_genes(saved_variants)
    locus_lists_by_guid = _add_locus_lists(project_models_by_guid.values(), genes, include_all_lists=True)

    projects_json = get_json_for_projects(project_models_by_guid.values(), user=request.user, add_project_category_guids_field=False)
    functional_tag_types = get_json_for_variant_functional_data_tag_types()

    variant_tag_types = VariantTagType.objects.filter(Q(project__in=project_models_by_guid.values()) | Q(project__isnull=True))
    prefetch_related_objects(variant_tag_types, 'project')
    variant_tags_json = _get_json_for_models(variant_tag_types)
    tag_projects = {vt.guid: vt.project.guid for vt in variant_tag_types if vt.project}

    for project_json in projects_json:
        project_guid = project_json['projectGuid']
        project_variant_tags = [
            vt for vt in variant_tags_json if tag_projects.get(vt['variantTagTypeGuid'], project_guid) == project_guid]
        project_json.update({
            'locusListGuids': locus_lists_by_guid.keys(),
            'variantTagTypes': sorted(project_variant_tags, key=lambda variant_tag_type: variant_tag_type['order']),
            'variantFunctionalTagTypes': functional_tag_types,
        })

    families_json = _get_json_for_families(list(families), user=request.user, add_individual_guids_field=True)
    individuals_json = _get_json_for_individuals(individuals, add_hpo_details=True, user=request.user)
    for locus_list in get_json_for_locus_lists(LocusList.objects.filter(guid__in=locus_lists_by_guid.keys()), request.user):
        locus_lists_by_guid[locus_list['locusListGuid']].update(locus_list)

    response_json.update({
        'genesById': genes,
        'projectsByGuid': {project['projectGuid']: project for project in projects_json},
        'familiesByGuid': {family['familyGuid']: family for family in families_json},
        'individualsByGuid': {indiv['individualGuid']: indiv for indiv in individuals_json},
        'locusListsByGuid': locus_lists_by_guid,
    })
    return create_json_response(response_json)


@staff_member_required(login_url=API_LOGIN_REQUIRED_URL)
@csrf_exempt
def upload_qc_pipeline_output(request):
    file_path = json.loads(request.body)['file']
    raw_records = parse_file(file_path, file_iter(file_path))

    json_records = [dict(zip(raw_records[0], row)) for row in raw_records[1:]]

    missing_columns = [field for field in ['seqr_id', 'data_type', 'filter_flags', 'qc_metrics_filters', 'qc_pop']
                       if field not in json_records[0]]
    if missing_columns:
        message = 'The following required columns are missing: {}'.format(', '.join(missing_columns))
        return create_json_response({'errors': [message]}, status=400, reason=message)

    dataset_types = {record['data_type'].lower() for record in json_records if record['data_type'].lower() != 'n/a'}
    if len(dataset_types) == 0:
        message = 'No dataset type detected'
        return create_json_response({'errors': [message]}, status=400, reason=message)
    elif len(dataset_types) > 1:
        message = 'Multiple dataset types detected: {}'.format(' ,'.join(dataset_types))
        return create_json_response({'errors': [message]}, status=400, reason=message)
    elif list(dataset_types)[0] not in DATASET_TYPE_MAP:
        message = 'Unexpected dataset type detected: "{}" (should be "exome" or "genome")'.format(list(dataset_types)[0])
        return create_json_response({'errors': [message]}, status=400, reason=message)

    dataset_type = DATASET_TYPE_MAP[list(dataset_types)[0]]

    info_message = 'Parsed {} {} samples'.format(len(json_records), dataset_type)
    logger.info(info_message)
    info = [info_message]
    warnings = []

    sample_ids = {record['seqr_id'] for record in json_records}
    samples = Sample.objects.filter(
        sample_id__in=sample_ids,
        sample_type=Sample.SAMPLE_TYPE_WES if dataset_type == 'exome' else Sample.SAMPLE_TYPE_WGS,
    ).exclude(
        individual__family__project__name__in=EXCLUDE_PROJECTS
    ).exclude(individual__family__project__projectcategory__name=EXCLUDE_PROJECT_CATEGORY)

    sample_individuals = {
        agg['sample_id']: agg['individuals'] for agg in
        samples.values('sample_id').annotate(individuals=ArrayAgg('individual_id', distinct=True))
    }

    sample_individual_max_loaded_date = {
        agg['individual_id']: agg['max_loaded_date'] for agg in
        samples.values('individual_id').annotate(max_loaded_date=Max('loaded_date'))
    }
    individual_latest_sample_id = {
        s.individual_id: s.sample_id for s in samples
        if s.loaded_date == sample_individual_max_loaded_date.get(s.individual_id)
    }

    for record in json_records:
        record['individual_ids'] = list({
            individual_id for individual_id in sample_individuals.get(record['seqr_id'], [])
            if individual_latest_sample_id[individual_id] == record['seqr_id']
        })

    missing_sample_ids = {record['seqr_id'] for record in json_records if not record['individual_ids']}
    if missing_sample_ids:
        individuals = Individual.objects.filter(individual_id__in=missing_sample_ids).exclude(
            family__project__name__in=EXCLUDE_PROJECTS).exclude(
            family__project__projectcategory__name=EXCLUDE_PROJECT_CATEGORY).exclude(
            sample__sample_type=Sample.SAMPLE_TYPE_WGS if dataset_type == 'exome' else Sample.SAMPLE_TYPE_WES)
        individual_db_ids_by_id = defaultdict(list)
        for individual in individuals:
            individual_db_ids_by_id[individual.individual_id].append(individual.id)
        for record in json_records:
            if not record['individual_ids'] and len(individual_db_ids_by_id[record['seqr_id']]) == 1:
                record['individual_ids'] = individual_db_ids_by_id[record['seqr_id']]
                missing_sample_ids.remove(record['seqr_id'])

    multi_individual_samples = {record['seqr_id']: len(record['individual_ids'])
                                for record in json_records if len(record['individual_ids']) > 1}
    if multi_individual_samples:
        logger.info('Found {} multi-individual samples from qc output'.format(len(multi_individual_samples)))
        warnings.append('The following {} samples were added to multiple individuals: {}'.format(
            len(multi_individual_samples), ', '.join(
                sorted(['{} ({})'.format(sample_id, count) for sample_id, count in multi_individual_samples.items()]))))

    if missing_sample_ids:
        logger.info('Missing {} samples from qc output'.format(len(missing_sample_ids)))
        warnings.append('The following {} samples were skipped: {}'.format(
            len(missing_sample_ids), ', '.join(sorted(list(missing_sample_ids)))))

    unknown_filter_flags = set()
    unknown_pop_filter_flags = set()

    inidividuals_by_population = defaultdict(list)
    for record in json_records:
        filter_flags = {}
        for flag in json.loads(record['filter_flags']):
            flag = '{}_{}'.format(flag, dataset_type) if flag == 'coverage' else flag
            flag_col = FILTER_FLAG_COL_MAP.get(flag, flag)
            if flag_col in record:
                filter_flags[flag] = record[flag_col]
            else:
                unknown_filter_flags.add(flag)

        pop_platform_filters = {}
        for flag in json.loads(record['qc_metrics_filters']):
            flag_col = 'sample_qc.{}'.format(flag)
            if flag_col in record:
                pop_platform_filters[flag] = record[flag_col]
            else:
                unknown_pop_filter_flags.add(flag)

        if filter_flags or pop_platform_filters:
            Individual.objects.filter(id__in=record['individual_ids']).update(
                filter_flags=filter_flags or None, pop_platform_filters=pop_platform_filters or None)

        inidividuals_by_population[record['qc_pop'].upper()] += record['individual_ids']

    for population, indiv_ids in inidividuals_by_population.items():
        Individual.objects.filter(id__in=indiv_ids).update(population=population)

    if unknown_filter_flags:
        message = 'The following filter flags have no known corresponding value and were not saved: {}'.format(
            ', '.join(unknown_filter_flags))
        logger.info(message)
        warnings.append(message)

    if unknown_pop_filter_flags:
        message = 'The following population platform filters have no known corresponding value and were not saved: {}'.format(
            ', '.join(unknown_pop_filter_flags))
        logger.info(message)
        warnings.append(message)

    message = 'Found and updated matching seqr individuals for {} samples'.format(len(json_records) - len(missing_sample_ids))
    info.append(message)
    logger.info(message)

    return create_json_response({
        'errors': [],
        'warnings': warnings,
        'info': info,
    })


FILTER_FLAG_COL_MAP = {
    'callrate': 'filtered_callrate',
    'contamination': 'PCT_CONTAMINATION',
    'chimera': 'AL_PCT_CHIMERAS',
    'coverage_exome': 'HS_PCT_TARGET_BASES_20X',
    'coverage_genome': 'WGS_MEAN_COVERAGE'
}

DATASET_TYPE_MAP = {
    'exome': 'exome',
    'genome': 'genome',
    'wes': 'exome',
    'wgs': 'genome',
}

EXCLUDE_PROJECTS = [
    '[DISABLED_OLD_CMG_Walsh_WES]', 'Old Engle Lab All Samples 352S', 'Old MEEI Engle Samples',
    'kl_temp_manton_orphan-diseases_cmg-samples_exomes_v1', 'Interview Exomes',
]
EXCLUDE_PROJECT_CATEGORY = 'Demo'

# Hop-by-hop HTTP response headers shouldn't be forwarded.
# More info at: http://www.w3.org/Protocols/rfc2616/rfc2616-sec13.html#sec13.5.1
EXCLUDE_HTTP_RESPONSE_HEADERS = {
    'connection', 'keep-alive', 'proxy-authenticate',
    'proxy-authorization', 'te', 'trailers', 'transfer-encoding', 'upgrade',
}

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


@staff_member_required(login_url=API_LOGIN_REQUIRED_URL)
@csrf_exempt
def proxy_to_kibana(request):
    headers = _convert_django_meta_to_http_headers(request.META)
    headers['Host'] = KIBANA_SERVER

    url = "{scheme}://{host}{path}".format(scheme=request.scheme, host=KIBANA_SERVER, path=request.get_full_path())

    request_method = getattr(requests.Session(), request.method.lower())

    try:
        # use stream=True because kibana returns gziped responses, and this prevents the requests module from
        # automatically unziping them
        response = request_method(url, headers=headers, data=request.body, stream=True, verify=True)
        response_content = response.raw.read()
        # make sure the connection is released back to the connection pool
        # (based on http://docs.python-requests.org/en/master/user/advanced/#body-content-workflow)
        response.close()

        proxy_response = HttpResponse(
            content=response_content,
            status=response.status_code,
            reason=response.reason,
            charset=response.encoding
        )

        for key, value in response.headers.iteritems():
            if key.lower() not in EXCLUDE_HTTP_RESPONSE_HEADERS:
                proxy_response[key.title()] = value

        return proxy_response
    except ConnectionError as e:
        logger.error(e)
        return HttpResponse("Error: Unable to connect to Kibana {}".format(e), status=400)


def _convert_django_meta_to_http_headers(request_meta_dict):
    """Converts django request.META dictionary into a dictionary of HTTP headers"""

    def convert_key(key):
        # converting Django's all-caps keys (eg. 'HTTP_RANGE') to regular HTTP header keys (eg. 'Range')
        return key.replace("HTTP_", "").replace('_', '-').title()

    http_headers = {
        convert_key(key): str(value).lstrip()
        for key, value in request_meta_dict.items()
        if key.startswith("HTTP_") or (key in ('CONTENT_LENGTH', 'CONTENT_TYPE') and value)
    }

    return http_headers
