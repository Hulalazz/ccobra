from contextlib import contextmanager
import os
import sys
import copy

import pandas as pd

import ccobra

import modelimporter
import comparator

@contextmanager
def dir_context(path):
    old_dir = os.getcwd()
    os.chdir(path)
    sys.path.append(path)
    try:
        yield
    finally:
        os.chdir(old_dir)
        sys.path.remove(path)

class Evaluator(object):
    def __init__(self, modellist, eval_comparator, test_datafile, train_datafile=None, silent=False, corresponding_data=False):
        """

        Parameters
        ----------
        corresponding_data : bool
            Indicates whether test and training data should contain the same
            user ids.

        """

        self.modellist = modellist
        self.silent = silent

        self.domains = set()
        self.response_types = set()

        self.comparator = eval_comparator

        # Load the datasets
        self.test_data = ccobra.data.CCobraData(pd.read_csv(test_datafile))
        self.domains.update(self.test_data.get()['domain'].unique())
        self.response_types.update(self.test_data.get()['response_type'].unique())

        self.train_data = None
        if train_datafile:
            self.train_data = ccobra.data.CCobraData(pd.read_csv(train_datafile))
            self.domains.update(self.train_data.get()['domain'].unique())
            self.response_types.update(self.train_data.get()['response_type'].unique())

            # If non-corresponding datasets, update with new identification
            if not corresponding_data:
                train_ids = self.train_data.get()['id'].unique()
                new_train_ids = dict(zip(train_ids, range(len(train_ids))))
                self.train_data.get()['id'].replace(new_train_ids, inplace=True)

                test_ids = self.test_data.get()['id'].unique()
                new_test_ids = dict(zip(test_ids, range(len(train_ids), len(train_ids) + len(test_ids))))
                self.test_data.get()['id'].replace(new_test_ids, inplace=True)

    def extract_optionals(self, data):
        essential = self.test_data.required_fields
        optionals = set(data.keys()) - set(essential)
        return {key: data[key] for key in optionals}

    def extract_demographics(self, data_df):
        demographics = {}
        demo_data = ['age', 'gender', 'education', 'affinity', 'experience']
        for data in demo_data:
            if data in data_df.columns:
                demographics[data] = data_df[data].unique().tolist()

                if len(demographics[data]) == 1:
                    demographics[data] = demographics[data][0]

        return demographics

    def evaluate(self):
        result_data = []

        # Activate model context
        for idx, model in enumerate(self.modellist):
            if not self.silent:
                print("Evaluating '{}' ({}/{})...".format(
                    model, idx + 1, len(self.modellist)))

            # Setup the model context
            context = os.path.dirname(os.path.abspath(model))
            with dir_context(context):
                importer = modelimporter.ModelImporter(
                    model, ccobra.CCobraModel)

                # Instantiate and prepare the model for predictions
                pre_model = importer.instantiate()

                # Check if model is applicable to domains/response types
                missing_domains = self.domains - set(pre_model.supported_domains)
                if len(missing_domains) > 0:
                    raise ValueError(
                        'Model {} is not applicable to domains {}.'.format(
                            pre_model.name, missing_domains))

                missing_response_types = self.response_types - set(pre_model.supported_response_types)
                if len(missing_response_types) > 0:
                    raise ValueError(
                        'Model {} is not applicable to response_types {}.'.format(
                            pre_model.name, missing_response_types))

                if self.train_data is not None:
                    # Prepare training data
                    train_data_dicts = []
                    for id_info, subj_df in self.train_data.get().groupby('id'):
                        subj_data = []
                        for seq_info, row in subj_df.sort_values(['sequence']).iterrows():
                            train_dict = {
                                'id': id_info,
                                'sequence': seq_info,
                                'item': ccobra.data.Item(
                                    id_info, row['domain'], row['task'],
                                    row['response_type'], row['choices'])
                            }

                            for key, value in row.iteritems():
                                if key not in train_dict:
                                    train_dict[key] = value

                            if isinstance(train_dict['response'], str):
                                if train_dict['response_type'] == 'multiple-choice':
                                    train_dict['response'] = [y.split(';') for y in [x.split('/') for x in train_dict['response'].split('|')]]
                                else:
                                    train_dict['response'] = [x.split(';') for x in train_dict['response'].split('/')]

                            subj_data.append(train_dict)
                        train_data_dicts.append(subj_data)

                    pre_model.pre_train(train_data_dicts)

                # Iterate subject
                for subj_id, subj_df in self.test_data.get().groupby('id'):
                    model = copy.deepcopy(pre_model)

                    # Extract the subject demographics
                    demographics = self.extract_demographics(subj_df)

                    # Set the models to new participant
                    model.start_participant(id=subj_id, **demographics)

                    for _, row in subj_df.sort_values('sequence').iterrows():
                        optionals = self.extract_optionals(row)

                        # Evaluation
                        sequence = row['sequence']
                        task = row['task']
                        choices = row['choices']
                        truth = row['response']
                        response_type = row['response_type']
                        domain = row['domain']

                        if isinstance(truth, str):
                            if response_type == 'multiple-choice':
                                truth = [y.split(';') for y in [x.split('/') for x in truth.split('|')]]
                            else:
                                truth = [x.split(';') for x in truth.split('/')]

                        item = ccobra.data.Item(
                            subj_id, domain, task, response_type, choices)

                        prediction = model.predict(item, **optionals)
                        hit = self.comparator.compare(prediction, truth)

                        # Adapt to true response
                        adapt_item = ccobra.data.Item(
                            subj_id, domain, task, response_type, choices)
                        model.adapt(adapt_item, truth, **optionals)

                        result_data.append({
                            'model': model.name,
                            'id': subj_id,
                            'domain': domain,
                            'sequence': sequence,
                            'task': task,
                            'choices': choices,
                            'truth': truth,
                            'prediction': comparator.tuple_to_string(prediction),
                            'hit': hit,
                        })

                # De-load the imported model and its dependencies. Might
                # cause garbage collection issues.
                importer.unimport()

        return pd.DataFrame(result_data)
