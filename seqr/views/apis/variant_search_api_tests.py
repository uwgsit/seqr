import json
import mock
from copy import deepcopy

from django.urls.base import reverse

from seqr.models import VariantSearchResults, LocusList, Project
from seqr.utils.elasticsearch.utils import InvalidIndexException
from seqr.views.apis.variant_search_api import query_variants_handler, query_single_variant_handler, \
    export_variants_handler, search_context_handler, get_saved_search_handler, create_saved_search_handler, \
    update_saved_search_handler, delete_saved_search_handler, get_variant_gene_breakdown
from seqr.views.utils.test_utils import AuthenticationTestCase, VARIANTS

LOCUS_LIST_GUID = 'LL00049_pid_genes_autosomal_do'
PROJECT_GUID = 'R0001_1kg'
SEARCH_HASH = 'd380ed0fd28c3127d07a64ea2ba907d7'
SEARCH = {'filters': {}, 'inheritance': None}
PROJECT_FAMILIES = [{'projectGuid': PROJECT_GUID, 'familyGuids': ['F000001_1', 'F000002_2']}]

VARIANTS_WITH_DISCOVERY_TAGS = deepcopy(VARIANTS)
VARIANTS_WITH_DISCOVERY_TAGS[2]['discoveryTags'] = [{
    'savedVariant': {
        'variantGuid': 'SV0000006_1248367227_r0003_tes',
        'familyGuid': 'F000011_11',
        'projectGuid': 'R0003_test',
    },
    'tagGuid': 'VT1726961_2103343353_r0003_tes',
    'name': 'Tier 1 - Novel gene and phenotype',
    'category': 'CMG Discovery Tags',
    'color': '#03441E',
    'searchHash': None,
    'lastModifiedDate': '2018-05-29T16:32:51.449Z',
    'createdBy': None,
}]


def _get_es_variants(results_model, **kwargs):
    results_model.save()
    return deepcopy(VARIANTS), len(VARIANTS)


def _get_empty_es_variants(results_model, **kwargs):
    results_model.save()
    return [], 0


class VariantSearchAPITest(AuthenticationTestCase):
    fixtures = ['users', '1kg_project', 'reference_data', 'variant_searches']
    multi_db = True

    @mock.patch('seqr.views.apis.variant_search_api.get_es_variant_gene_counts')
    @mock.patch('seqr.views.apis.variant_search_api.get_es_variants')
    def test_query_variants(self, mock_get_variants, mock_get_gene_counts):
        url = reverse(query_variants_handler, args=['abc'])
        self.check_collaborator_login(url, request_data={'projectFamilies': PROJECT_FAMILIES})
        url = reverse(query_variants_handler, args=[SEARCH_HASH])

        # add a locus list
        LocusList.objects.get(guid=LOCUS_LIST_GUID).projects.add(Project.objects.get(guid=PROJECT_GUID))

        # Test invalid inputs
        response = self.client.get(url)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.reason_phrase, 'Invalid search hash: {}'.format(SEARCH_HASH))

        response = self.client.post(url, content_type='application/json', data=json.dumps({'search': SEARCH}))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.reason_phrase, 'Invalid search: no projects/ families specified')

        mock_get_variants.side_effect = InvalidIndexException('Invalid index')
        response = self.client.post(url, content_type='application/json', data=json.dumps({
            'projectFamilies': PROJECT_FAMILIES, 'search': SEARCH
        }))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.reason_phrase, 'Invalid index')

        mock_get_variants.side_effect = _get_es_variants

        # Test new search
        response = self.client.post(url, content_type='application/json', data=json.dumps({
            'projectFamilies': PROJECT_FAMILIES, 'search': SEARCH
        }))
        self.assertEqual(response.status_code, 200)
        response_json = response.json()
        self.assertSetEqual(set(response_json.keys()), {
            'searchedVariants', 'savedVariantsByGuid', 'genesById', 'search', 'variantTagsByGuid', 'variantNotesByGuid',
            'variantFunctionalDataByGuid', 'locusListsByGuid'})
        self.assertListEqual(response_json['searchedVariants'], VARIANTS)
        self.assertDictEqual(response_json['search'], {
            'search': SEARCH,
            'projectFamilies': PROJECT_FAMILIES,
            'totalResults': 3,
        })
        self.assertSetEqual(
            set(response_json['savedVariantsByGuid'].keys()),
            {'SV0000001_2103343353_r0390_100', 'SV0000002_1248367227_r0390_100'}
        )
        self.assertSetEqual(
            set(response_json['genesById'].keys()),
            {'ENSG00000227232', 'ENSG00000268903', 'ENSG00000233653'}
        )
        self.assertListEqual(
            response_json['genesById']['ENSG00000227232']['locusListGuids'], [LOCUS_LIST_GUID]
        )
        self.assertSetEqual(set(response_json['locusListsByGuid'].keys()), {LOCUS_LIST_GUID})
        intervals = response_json['locusListsByGuid'][LOCUS_LIST_GUID]['intervals']
        self.assertEqual(len(intervals), 2)
        self.assertSetEqual(
            set(intervals[0].keys()), {'locusListGuid', 'locusListIntervalGuid', 'genomeVersion', 'chrom', 'start', 'end'}
        )

        results_model = VariantSearchResults.objects.get(search_hash=SEARCH_HASH)
        mock_get_variants.assert_called_with(results_model, sort='xpos', page=1, num_results=100)

        # Test pagination
        response = self.client.get('{}?page=3'.format(url))
        self.assertEqual(response.status_code, 200)
        mock_get_variants.assert_called_with(results_model, sort='xpos', page=3, num_results=100)

        # Test sort
        response = self.client.get('{}?sort=consequence'.format(url))
        self.assertEqual(response.status_code, 200)
        mock_get_variants.assert_called_with(results_model, sort='consequence', page=1, num_results=100)

        # Test export
        export_url = reverse(export_variants_handler, args=[SEARCH_HASH])
        response = self.client.get(export_url)
        self.assertEqual(response.status_code, 200)
        export_content = [row.split('\t') for row in response.content.rstrip('\n').split('\n')]
        self.assertEqual(len(export_content), 4)
        self.assertListEqual(
            export_content[0],
            ['chrom', 'pos', 'ref', 'alt', 'gene', 'worst_consequence', '1kg_freq', 'exac_freq', 'gnomad_genomes_freq',
            'gnomad_exomes_freq', 'topmed_freq', 'cadd', 'revel', 'eigen', 'polyphen', 'sift', 'muttaster', 'fathmm',
             'rsid', 'hgvsc', 'hgvsp', 'clinvar_clinical_significance', 'clinvar_gold_stars', 'filter', 'family_id_1',
             'tags_1', 'notes_1', 'family_id_2', 'tags_2', 'notes_2', 'sample_1:num_alt_alleles:gq:ab', 'sample_2:num_alt_alleles:gq:ab'])
        self.assertListEqual(
            export_content[1],
            ['21', '3343400', 'GAGA', 'G', 'WASH7P', 'missense_variant', '', '', '', '', '', '', '', '', '', '', '', '',
             '', 'ENST00000623083.3:c.1075G>A', 'ENSP00000485442.1:p.Gly359Ser', '', '', '', '1',
             'Tier 1 - Novel gene and phenotype (None)|Review (None)', '', '2', '', '', 'NA19675:1:46.0:0.702127659574', 'NA19679:0:99.0:0.0'])
        self.assertListEqual(
            export_content[3],
            ['12', '48367227', 'TC', 'T', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '',
             '', '2', 'Known gene for phenotype (None)|Review (None)', 'test n\xc3\xb8te (None)', '', '', '', '', ''])

        mock_get_variants.assert_called_with(results_model, page=1, load_all=True)

        # Test gene breakdown
        gene_counts = {
            'ENSG00000227232': {'total': 2, 'families': {'F000001_1': 2, 'F000002_2': 1}},
            'ENSG00000268903': {'total': 1, 'families': {'F000002_2': 1}}
        }
        mock_get_gene_counts.return_value = gene_counts

        gene_breakdown_url = reverse(get_variant_gene_breakdown, args=[SEARCH_HASH])
        response = self.client.get(gene_breakdown_url)
        self.assertEqual(response.status_code, 200)
        response_json = response.json()
        self.assertSetEqual(set(response_json.keys()), {'searchGeneBreakdown', 'genesById'})
        self.assertDictEqual(response_json['searchGeneBreakdown'], {SEARCH_HASH: gene_counts})
        self.assertSetEqual(set(response_json['genesById'].keys()), {'ENSG00000227232', 'ENSG00000268903'})

        # Test cross-project discovery for staff users
        self.login_staff_user()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        response_json = response.json()
        self.assertSetEqual(set(response_json.keys()), {
            'searchedVariants', 'savedVariantsByGuid', 'genesById', 'search', 'variantTagsByGuid', 'variantNotesByGuid',
            'variantFunctionalDataByGuid', 'familiesByGuid', 'locusListsByGuid'})

        self.assertListEqual(response_json['searchedVariants'], VARIANTS_WITH_DISCOVERY_TAGS)
        self.assertSetEqual(set(response_json['familiesByGuid'].keys()), {'F000011_11'})

        # Test no results
        mock_get_variants.side_effect = _get_empty_es_variants
        response = self.client.post(url, content_type='application/json', data=json.dumps({
            'projectFamilies': PROJECT_FAMILIES, 'search': SEARCH
        }))
        self.assertEqual(response.status_code, 200)
        response_json = response.json()
        self.assertDictEqual(response_json, {
            'searchedVariants': [],
            'search': {
                'search': SEARCH,
                'projectFamilies': PROJECT_FAMILIES,
                'totalResults': 0,
            }
        })

    def test_search_context(self):
        search_context_url = reverse(search_context_handler)
        self.check_collaborator_login(search_context_url, request_data={'familyGuid': 'F000001_1'})

        response = self.client.post(search_context_url, content_type='application/json', data=json.dumps({'foo': 'bar'}))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.reason_phrase, 'Invalid context params: {"foo": "bar"}')

        response = self.client.post(search_context_url, content_type='application/json', data=json.dumps({'projectGuid': PROJECT_GUID}))
        self.assertEqual(response.status_code, 200)
        response_json = response.json()
        self.assertSetEqual(
            set(response_json),
            {'savedSearchesByGuid', 'projectsByGuid', 'familiesByGuid', 'individualsByGuid', 'samplesByGuid',
             'igvSamplesByGuid', 'locusListsByGuid', 'analysisGroupsByGuid', }
        )
        self.assertEqual(len(response_json['savedSearchesByGuid']), 3)
        self.assertTrue(PROJECT_GUID in response_json['projectsByGuid'])
        self.assertTrue('F000001_1' in response_json['familiesByGuid'])
        self.assertTrue('AG0000183_test_group' in response_json['analysisGroupsByGuid'])

        response = self.client.post(search_context_url, content_type='application/json', data=json.dumps({'familyGuid': 'F000001_1'}))
        self.assertEqual(response.status_code, 200)
        response_json = response.json()
        self.assertSetEqual(
            set(response_json),
            {'savedSearchesByGuid', 'projectsByGuid', 'familiesByGuid', 'individualsByGuid', 'samplesByGuid',
             'igvSamplesByGuid', 'locusListsByGuid', 'analysisGroupsByGuid', }
        )
        self.assertEqual(len(response_json['savedSearchesByGuid']), 3)
        self.assertTrue(PROJECT_GUID in response_json['projectsByGuid'])
        self.assertTrue('F000001_1' in response_json['familiesByGuid'])
        self.assertTrue('AG0000183_test_group' in response_json['analysisGroupsByGuid'])

        response = self.client.post(search_context_url, content_type='application/json', data=json.dumps({'analysisGroupGuid': 'AG0000183_test_group'}))
        self.assertEqual(response.status_code, 200)
        response_json = response.json()
        self.assertSetEqual(
            set(response_json),
            {'savedSearchesByGuid', 'projectsByGuid', 'familiesByGuid', 'individualsByGuid', 'samplesByGuid',
             'igvSamplesByGuid', 'locusListsByGuid', 'analysisGroupsByGuid', }
        )
        self.assertEqual(len(response_json['savedSearchesByGuid']), 3)
        self.assertTrue(PROJECT_GUID in response_json['projectsByGuid'])
        self.assertTrue('F000001_1' in response_json['familiesByGuid'])
        self.assertTrue('AG0000183_test_group' in response_json['analysisGroupsByGuid'])

        response = self.client.post(search_context_url, content_type='application/json', data=json.dumps({'projectCategoryGuid': 'PC000003_test_category_name'}))
        self.assertEqual(response.status_code, 200)
        response_json = response.json()
        self.assertSetEqual(
            set(response_json),
            {'savedSearchesByGuid', 'projectsByGuid', 'familiesByGuid', 'individualsByGuid', 'samplesByGuid',
             'igvSamplesByGuid', 'locusListsByGuid', 'analysisGroupsByGuid', 'projectCategoriesByGuid'}
        )
        self.assertEqual(len(response_json['savedSearchesByGuid']), 3)
        self.assertTrue(PROJECT_GUID in response_json['projectsByGuid'])
        self.assertTrue('F000001_1' in response_json['familiesByGuid'])
        self.assertTrue('AG0000183_test_group' in response_json['analysisGroupsByGuid'])
        self.assertListEqual(response_json['projectCategoriesByGuid'].keys(), ['PC000003_test_category_name'])

        # Test search hash context
        response = self.client.post(search_context_url, content_type='application/json', data=json.dumps(
            {'searchHash': SEARCH_HASH}))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.reason_phrase, 'Invalid search hash: {}'.format(SEARCH_HASH))

        response = self.client.post(search_context_url, content_type='application/json', data=json.dumps(
            {'searchHash': SEARCH_HASH, 'searchParams': {'search': SEARCH}}))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.reason_phrase, 'Invalid search: no projects/ families specified')

        response = self.client.post(search_context_url, content_type='application/json', data=json.dumps(
            {'searchHash': SEARCH_HASH, 'searchParams': {'projectFamilies': PROJECT_FAMILIES, 'search': SEARCH}}))
        self.assertEqual(response.status_code, 200)
        response_json = response.json()
        self.assertSetEqual(
            set(response_json),
            {'savedSearchesByGuid', 'projectsByGuid', 'familiesByGuid', 'individualsByGuid', 'samplesByGuid',
             'igvSamplesByGuid', 'locusListsByGuid', 'analysisGroupsByGuid', }
        )
        self.assertEqual(len(response_json['savedSearchesByGuid']), 3)
        self.assertTrue(PROJECT_GUID in response_json['projectsByGuid'])
        self.assertTrue('F000001_1' in response_json['familiesByGuid'])

        response = self.client.post(search_context_url, content_type='application/json', data=json.dumps(
            {'searchHash': SEARCH_HASH}))
        self.assertEqual(response.status_code, 200)
        response_json = response.json()
        self.assertSetEqual(
            set(response_json),
            {'savedSearchesByGuid', 'projectsByGuid', 'familiesByGuid', 'individualsByGuid', 'samplesByGuid',
             'igvSamplesByGuid', 'locusListsByGuid', 'analysisGroupsByGuid', }
        )
        self.assertEqual(len(response_json['savedSearchesByGuid']), 3)
        self.assertTrue(PROJECT_GUID in response_json['projectsByGuid'])
        self.assertTrue('F000001_1' in response_json['familiesByGuid'])

    @mock.patch('seqr.views.apis.variant_search_api.get_single_es_variant')
    def test_query_single_variant(self, mock_get_variant):
        mock_get_variant.return_value = VARIANTS[0]

        url = '{}?familyGuid=F000001_1'.format(reverse(query_single_variant_handler, args=['21-3343353-GAGA-G']))
        self.check_collaborator_login(url)

        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        response_json = response.json()
        self.assertSetEqual(
            set(response_json.keys()),
            {'searchedVariants', 'savedVariantsByGuid', 'genesById', 'projectsByGuid', 'familiesByGuid',
             'individualsByGuid', 'samplesByGuid', 'locusListsByGuid', 'analysisGroupsByGuid', 'variantTagsByGuid',
             'variantNotesByGuid', 'variantFunctionalDataByGuid', 'igvSamplesByGuid', }
        )

        self.assertListEqual(response_json['searchedVariants'], VARIANTS[:1])
        self.assertSetEqual(set(response_json['savedVariantsByGuid'].keys()), {'SV0000001_2103343353_r0390_100'})
        self.assertSetEqual(set(response_json['genesById'].keys()), {'ENSG00000227232', 'ENSG00000268903'})
        self.assertTrue('F000001_1' in response_json['familiesByGuid'])

    def test_saved_search(self):
        get_saved_search_url = reverse(get_saved_search_handler)
        self.check_require_login(get_saved_search_url)

        response = self.client.get(get_saved_search_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['savedSearchesByGuid']), 3)

        create_saved_search_url = reverse(create_saved_search_handler)

        response = self.client.post(create_saved_search_url, content_type='application/json', data='{}')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.reason_phrase, '"Name" is required')

        body = {'name': 'Test Search'}

        invalid_body = {'inheritance': {'filter': {'genotype': {'indiv_1': 'ref_alt'}}}}
        invalid_body.update(body)
        response = self.client.post(create_saved_search_url, content_type='application/json', data=json.dumps(invalid_body))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.reason_phrase, 'Saved searches cannot include custom genotype filters')

        body.update(SEARCH)
        response = self.client.post(create_saved_search_url, content_type='application/json', data=json.dumps(body))
        self.assertEqual(response.status_code, 200)
        saved_searches = response.json()['savedSearchesByGuid']
        self.assertEqual(len(saved_searches), 1)
        search_guid = saved_searches.keys()[0]
        self.assertDictEqual(saved_searches[search_guid], {
            'savedSearchGuid': search_guid, 'name': 'Test Search', 'search': SEARCH, 'createdById': 13,
        })

        response = self.client.get(get_saved_search_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['savedSearchesByGuid']), 4)

        update_saved_search_url = reverse(update_saved_search_handler, args=[search_guid])
        body['name'] = 'Updated Test Search'
        response = self.client.post(update_saved_search_url, content_type='application/json', data=json.dumps(body))
        self.assertEqual(response.status_code, 200)
        self.assertDictEqual(response.json()['savedSearchesByGuid'][search_guid], {
            'savedSearchGuid': search_guid, 'name': 'Updated Test Search', 'search': SEARCH, 'createdById': 13,
        })

        delete_saved_search_url = reverse(delete_saved_search_handler, args=[search_guid])
        response = self.client.get(delete_saved_search_url)
        self.assertEqual(response.status_code, 200)
        self.assertDictEqual(response.json(), {'savedSearchesByGuid': {search_guid: None}})

        response = self.client.get(get_saved_search_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['savedSearchesByGuid']), 3)

        global_saved_search_guid = response.json()['savedSearchesByGuid'].keys()[0]

        update_saved_search_url = reverse(update_saved_search_handler, args=[global_saved_search_guid])
        response = self.client.post(update_saved_search_url, content_type='application/json', data=json.dumps(body))
        self.assertEqual(response.status_code, 403)

        delete_saved_search_url = reverse(delete_saved_search_handler, args=[global_saved_search_guid])
        response = self.client.get(delete_saved_search_url)
        self.assertEqual(response.status_code, 403)
