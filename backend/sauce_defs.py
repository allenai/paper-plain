import requests
import re
import tqdm
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup as bs
import os
import sys
import pandas as pd
import json
import uuid
import datasets

import spacy
import scispacy

from elasticsearch import Elasticsearch
import time

import inflect


CACHE='/net/nfs2.s2-research/tala/.cache/huggingface/'
DATA_DIR = '/home/tala/s2-simplify/data'
os.environ['TRANSFORMERS_CACHE'] = CACHE

es_client = Elasticsearch([{'host': 'localhost', 'port': '9200'}])
# wiki = datasets.load_dataset('wikipedia', "20200501.en", cache_dir='{}/datasets/'.format(CACHE))
# wiki['train'].load_elasticsearch_index("title", host="localhost", port="9200", es_index_name="wiki_train")

inflect_engine = inflect.engine()
sci_nlp = spacy.load("en_core_sci_scibert")


#### filtering with BERN 
def query_raw(text, url="https://bern.korea.ac.kr/plain"):
    return requests.post(url, data={'sample_text': text}).json()

def query_bern(term):
    time.sleep(1) # so that it doesn't throttle the request
    response = query_raw(term)
    if 'denotations' not in response.keys() or len(response['denotations']) < 1:
        return None
    return response['denotations'][0]



def get_medplus_response(term):
    time.sleep(1)
    base_url = "https://wsearch.nlm.nih.gov/ws/query/?db=healthTopics"
    
    response = requests.get("{}&term={}".format(base_url, term))
    
    response_root = ET.fromstring(response.text)
        
    return response_root


# for now this just gets the first result always
def get_medplus(term):
    response_root = get_medplus_response(term)

    list_results = response_root.find('list')
    
    if list_results == None:
        return None
    
    first_result =  list_results[0]
    
    # find title and summary
    title = None
    summary = None
    
    for c in first_result:
        if c.attrib['name'] == 'FullSummary':
            summary = c
        elif c.attrib['name'] == 'title':
            title = c

    soup_text = bs(summary.text)
        
    # this returns the first sentence of each of the paragraphs of the M+ returned text
    return '. '.join([p.get_text().split('.')[0] for p in soup_text.find_all('p')])

# For the rest of the terms, see if the entity also exists in the first sentence of the wikipedia article. 
def get_wiki_article(s, wiki_ds, k):
    
    # collect wikipedia entries 
    scores, answers = wiki_ds.get_nearest_examples('title', s, k=k)
    
    # loop through answers and take the first one that contains the full term in the first sentence
    first_sentences = [a.split('.')[0] for a in answers['text']]
    for sent in first_sentences:
        if s in sent:
            return sent
    
    # if none of them contain the term (or there are no sentences) return None
    return None

def select_def(row):
    # if the row represents a BERN ent, then use the m+ def if it's there, or else wiki
    if row['BERN'] != None and row['medlinePlus'] != None:
        return (row['medlinePlus'], 'Medline')
    
    # else use wiki
    return (row['wikipedia'], 'Wikipedia')

def make_singular(term):
    singular_term = inflect_engine.singular_noun(term)
    if singular_term == False:
        return term
    else:
        return singular_term
    
def make_clean_term(term):
    return make_singular(re.sub('\.|;|,|\(|\)', '', term.lower()))
        
def make_term_df(matching_tokens):
    # make the text spans for the tokens
    matched_tokens = [t['tokens'] for t in matching_tokens]

    matched_tokens_text = []
    for span in matched_tokens:
        matched_tokens_text.append(' '.join([t['text'] for t in span]))

    # clean the token text a bit 
    # this doesn't have to be perfect because anything we don't have a def for we don't include
    cleaned_matched_tokens_text = [make_clean_term(s) for s in matched_tokens_text]
    

    df_tokens = pd.DataFrame(list(zip(matched_tokens_text, cleaned_matched_tokens_text)), columns = ['term', 'cleaned_term'])
    
    return df_tokens

def make_definitions_df(matching_tokens):
    
    df_tokens = make_term_df(matching_tokens)

    # first collect all the m+
    df_tokens['medlinePlus'] = [get_medplus(t) for t in tqdm.tqdm(df_tokens['cleaned_term'])]

    # because the m+ ones are hit or miss, filter with bern to make sure the term refers to a biomed ent
    print("Getting BERN, this might take a moment....", end=' ')
    df_tokens['BERN'] = [query_bern(t) for t in df_tokens['cleaned_term']]   
    print('Done!')

    # then wikipedia
    df_tokens['wikipedia'] = [get_wiki_article(s, wiki['train'], 1) for s in df_tokens['cleaned_term']]   


    return df_tokens

def get_def(term, definitions):
    cleaned_term = make_clean_term(term)
    df_term = definitions[(definitions['term'] == term) | (definitions['cleaned_term'] == cleaned_term)]
    if len(df_term) > 0:
        return df_term.iloc[0]['definition']    
    return None