import re
import string
import json
from collections import namedtuple

import yaml
import pandas as pd
from bs4 import BeautifulSoup
from nltk.corpus import stopwords
from yargy.tokenizer import MorphTokenizer
from razdel import sentenize

from disambiguator.db import RutezDB

with open("config.yml", 'r') as config_file:
    config = yaml.load(config_file)

# todo: rise relation threshold to 3-4 to increase recall
# todo: words in the same grammar form have same meaning ?
# todo: words only in different sentences ?

Item = namedtuple('Item', [
    'id',
    'text',
    'type',
    'span',
    'normal_form',
    'is_polysemous',
    'is_stopword'
])


class TextProcessor:
    def __init__(self, database):
        self.db = RutezDB(database)
        self.morph_tokenizer = MorphTokenizer()
        self.stop_words = stopwords.words('russian') + [s for s in string.punctuation]

    @staticmethod
    def extract_text_from_html(text):
        soup = BeautifulSoup(text, 'html.parser')
        [s.extract() for s in soup('nomorph')]
        [s.extract() for s in soup('title')]
        [s.append('.') for s in soup(re.compile('^h[1-6]$'))]
        text = soup.get_text().replace('\n', ' ').lstrip()
        return text

    def morph_tokenize(self, text):
        result = []
        text = text.lower()
        tokens = list(self.morph_tokenizer(text))
        query_items = [token.normalized for token in tokens if token.value not in self.stop_words]
        ids = self.db.select_words_db_ids(query_items)
        for token in tokens:
            id_, is_poly = ids.get(token.normalized, (None, None))
            if token.value not in self.stop_words:
                if hasattr(token, 'forms'):
                    word_type = [form for form in list(token.forms[0].grams.values) if form.isupper()][0]
                    if word_type in ['INFN', 'PRTF', 'GRND', 'PRTS']:
                        word_type = 'VERB'
                    if word_type == 'ADJF':
                        word_type = 'ADJ'
                result.append(
                    Item(
                        id_,
                        token.value,
                        word_type or None,
                        token.span,
                        token.normalized,
                        is_poly,
                        token.normalized is stopwords
                    )
                )
        return result

    @staticmethod
    def _make_text_window(idx, size, min_idx, max_idx, tokens, type_, skip=None):
        window = []
        window_start = max(min_idx, idx - size)
        window_end = min(max_idx, idx + size)
        if type_ == 'token':
            window = tokens[window_start: window_end]
        elif type_ == 'text':
            window = [t.normal_form for t in tokens[window_start: window_end] if t != skip]
        elif type_ == 'form':
            window = [t.normal_form + '_' + t.type for t in tokens[window_start: window_end] if t != skip]
        return window

    # todo: implement k
    @staticmethod
    def _find_containing_sentences(sentences, token, k):
        containing_sentence = [
            sentence.text
            for sentence in sentences
            if sentence.start <= token.span[0] and sentence.stop >= token.span[1]
        ][0]
        return containing_sentence
    
    def predict_simple(self, json_corpus, window_size=10, context_window_size=2, scorer=None, scorer_params=None):
        corpus = json.load(open(json_corpus, 'r'))
        result = []
        for document_idx, document in enumerate(corpus):
            tokens = self.morph_tokenize(document)
            document_length = len(tokens)
            sentences = list(sentenize(document))

            query_items = [str(token.id) for token in tokens if token.is_polysemous == 1]
            close_words, close_words_to_meaning, meaning_id_to_word = self.db.select_close_words(query_items)

            for token_idx, token in enumerate(tokens):
                if token.is_polysemous == 1:
                    containing_sentence = self._find_containing_sentences(sentences, token, k=1)
                    token_close_words = close_words.get(token.id)
                    search_window = self._make_text_window(
                        token_idx, window_size, 0, document_length, tokens, 'token'
                    )
                    token_context_window = self._make_text_window(
                        token_idx, context_window_size, 0, document_length, tokens, 'form', skip=token
                    )

                    for search_token_idx, search_token in enumerate(search_window):
                        if search_token.is_polysemous == 0 and search_token.id in token_close_words:
                            meaning_id = close_words_to_meaning[token.id][search_token.id]
                            meaning = meaning_id_to_word[meaning_id]
                            
                            search_context_window = self._make_text_window(
                                search_token_idx, context_window_size, 0, document_length, tokens, 'form', search_token
                            )
                            
                            if scorer:
                                score = scorer(search_context_window, token_context_window, scorer_params)
                            else:
                                score = 0

                            result.append(
                                dict(
                                    document=document_idx,
                                    text_position=str(list(token.span)),
                                    meaning=meaning,
                                    baseline=score,
                                    text=containing_sentence
                                )
                            )

            result = pd.DataFrame(result)
            result.drop_duplicates(inplace=True)
            return result

    @staticmethod
    def precision_recall_score(df_pred, df_true, score_threshold=0):
        merged = df_true.merge(df_pred, on=['document', 'text_position'])

        if score_threshold == 0:
            true_pred = merged[merged['meaning'] == merged['annotation']].shape[0]
            total_pred = merged.shape[0]
        else:
            true_pred = merged[(merged['meaning'] == merged['annotation']) &
                               (merged['baseline'] >= score_threshold)].shape[0]
            total_pred = merged[merged['baseline'] >= score_threshold].shape[0]
        print(true_pred, total_pred)
        precision = true_pred * 100 / total_pred

        merged.drop('meaning', axis=1, inplace=True)
        merged.drop_duplicates(inplace=True)

        # todo: fix?
        recall = total_pred * 100 / df_true.shape[0]
        f_score = 2 * precision * recall / (precision + recall)
        return precision, recall, f_score