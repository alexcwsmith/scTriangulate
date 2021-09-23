import sys
import os
import copy
import pickle
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib import cm
import matplotlib as mpl
import seaborn as sns
from anytree import Node, RenderTree
from scipy.sparse import issparse,csr_matrix
from scipy.spatial.distance import pdist,squareform
from scipy.cluster.hierarchy import linkage,leaves_list
import multiprocessing as mp
import platform
import logging
import subprocess
import re

import scanpy as sc
import anndata as ad
import gseapy as gp
import scrublet as scr

from .shapley import *
from .metrics import *
from .viewer import *
from .prune import *
from .colors import *
from .preprocessing import *


import matplotlib as mpl

# for publication ready figure
mpl.rcParams['pdf.fonttype'] = 42
mpl.rcParams['ps.fonttype'] = 42
mpl.rcParams['font.family'] = 'Arial'

# # for publication and super large dataset
# mpl.rcParams['savefig.dpi'] = 600
# mpl.rcParams['figure.dpi'] = 600



# define ScTriangulate Object
class ScTriangulate(object):

    '''
    ScTriangulate object.

    Example::

        adata = sc.read('pbmc3k_azimuth_umap.h5ad')
        sctri = ScTriangulate(dir='./output',adata=adata,query=['leiden1','leiden2','leiden3'])

    :param dir: Output folder path on the disk
    :param adata: input adata file
    :param query: a python list contains the annotation names to query

    '''

    def __init__(self,dir,adata,query,species='human',criterion=2,verbose=1,reference=None,add_metrics={'tfidf5':tf_idf5_for_cluster},
                    predict_doublet=True):

        self.verbose = verbose
        self.dir = dir
        self._create_dir_if_not_exist()
        self.adata = adata
        self.query = query
        if reference is None:
            self.reference = self.query[0]
        else:
            self.reference = reference
        self.species = species
        self.criterion = criterion
        self.score = {}
        self.cluster = {}
        self.uns = {}
        self.metrics = ['reassign','tfidf10','SCCAF','doublet']   # default metrics
        self.add_metrics = {}                         # user can add their own, key is metric name, value is callable
        self.total_metrics = self.metrics               # all metrics considered

        self._set_logging()          
        self._check_adata()
        self.size_dict, _ = get_size(self.adata.obs,self.query)
        self.invalid = []

        # run doublet predict by default in the initialization
        if predict_doublet:
            if not predict_doublet == 'precomputed':
                self.doublet_predict()
        else:
            doublet_scores = np.full(shape=self.adata.obs.shape[0],fill_value=0.5)  # add a dummy score
            self.adata.obs['doublet_scores'] = doublet_scores

        # add add_metrics by default in the initialization
        self.add_new_metrics(add_metrics)


    def __str__(self):  # when you print(instance) in REPL
        return 'ScTriangualate Object:\nWorking directory is {0}\nQuery Annotation: {1}\nReference Annotation: {2}\n'\
            'Species: {3}\nCriterion: {4}\nTotal Metrics: {5}\nScore slot contains: {6}\nCluster slot contains: {7}\nUns slot contains: {8}\n'\
            'Invalid cluster: {9}'.format(self.dir, self.query,self.reference,self.species,self.criterion,self.total_metrics, list(self.score.keys()),
            list(self.cluster.keys()),list(self.uns.keys()),self.invalid)

    def __repr__(self):  # when you type the instance in REPL
        return 'ScTriangualate Object:\nWorking directory is {0}\nQuery Annotation: {1}\nReference Annotation: {2}\n'\
            'Species: {3}\nCriterion: {4}\nTotal Metrics: {5}\nScore slot contains: {6}\nCluster slot contains: {7}\nUns slot contains: {8}\n'\
            'Invalid cluster: {9}'.format(self.dir, self.query,self.reference,self.species,self.criterion,self.total_metrics, list(self.score.keys()),
            list(self.cluster.keys()),list(self.uns.keys()),self.invalid)

    def _create_dir_if_not_exist(self):
        if not os.path.exists(self.dir):
            os.mkdir(self.dir)

    def _check_adata(self):
        # step1: make all cluster name str
        if self.reference in self.query:
            all_keys = self.query
        else:
            all_keys = copy.deepcopy(self.query)
            all_keys.append(self.reference)
        for key in all_keys:
            self.adata.obs[key] = self.adata.obs[key].astype('str')
            self.adata.obs[key] = self.adata.obs[key].astype('category')
        # step2: replace invalid char in cluster and key name    
        ## replace cluster name
        invalid_chars = ['/','@','$',' ']
        if self.reference in self.query:
            all_keys = self.query
        else:
            all_keys = copy.deepcopy(self.query)
            all_keys.append(self.reference)
        for key in all_keys:
            for ichar in invalid_chars:
                self.adata.obs[key] = self.adata.obs[key].str.replace(ichar,'_')
        
        ## replace key name
        for key in all_keys:
            for ichar in invalid_chars:
                self.adata.obs.rename(columns={key:key.replace(ichar,'_')},inplace=True)   
        # step3: remove index name for smooth h5ad writing
        self.adata.obs.index.name = None
        self.adata.var.index.name = None

    def _set_logging(self):
        # get all logger
        global logger_sctriangulate
        logger_sctriangulate = logging.getLogger(__name__)
        logger_scanpy = logging.getLogger('scanpy')
        logger_gseapy = logging.getLogger('gseapy')
        logger_scrublet = logging.getLogger('scrublet')

        # make other logger silent
        logger_scanpy.setLevel(logging.ERROR)
        logger_gseapy.setLevel(logging.ERROR)
        logger_scrublet.setLevel(logging.ERROR)

        # configure own logger
        if self.verbose == 1:
            c_handler = logging.StreamHandler()
            c_handler.setLevel(logging.INFO)
            c_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s' )
            c_handler.setFormatter(c_formatter)        
            logger_sctriangulate.addHandler(c_handler)
            logger_sctriangulate.info('choosing console logging')

        elif self.verbose == 2:
            if not os.path.exists(self.dir):
                os.mkdir(self.dir)
            f_handler = logging.FileHandler(os.path.join(self.dir,'scTriangulate.log'))
            f_handler.setLevel(logging.INFO)
            f_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s' )
            f_handler.setFormatter(f_formatter)
            logger_sctriangulate.addHandler(f_handler)
            logger_sctriangulate.info('choosing file logging')


    def _to_dense(self):
        self.adata.X = self.adata.X.toarray() 
        
    def _to_sparse(self):
        self.adata.X = csr_matrix(self.adata.X)

    def obs_to_df(self,name='sctri_inspect_obs.txt'):
        self.adata.obs.to_csv(os.path.join(self.dir,name),sep='\t')

    def var_to_df(self,name='sctri_inspect_var.txt'):
        self.adata.var.to_csv(os.path.join(self.dir,name),sep='\t')

    def gene_to_df(self,mode,key,raw=False,col='purify',n=100):
        '''mode is marker_genes or exclusive_genes'''
        if not raw: # reformat the output to human readable
            df = self.uns['{}'.format(mode)][key]
            if mode == 'marker_genes':
                result = pd.Series()
                for i in range(df.shape[0]):
                    cluster = df.index[i]
                    markers = df.iloc[i][col]
                    single_column = pd.Series(data=markers,name=cluster)
                    result = pd.concat([result,single_column],axis=1,ignore_index=True)
                result.drop(columns=0,inplace=True)
                all_clusters = df.index
                result.columns = all_clusters

            elif mode == 'exclusive_genes':
                result = pd.DataFrame({'cluster':[],'gene':[],'score':[]})
                for i in range(df.shape[0]): #  here the exclusive gene df is actually a series
                    cluster = df.index[i]
                    gene = df[i]
                    col_cluster = np.full(n,fill_value=cluster)
                    col_gene = list(gene.keys())[:n]
                    col_score = list(gene.values())[:n]
                    chunk = pd.DataFrame({'cluster':col_cluster,'gene':col_gene,'score':col_score})
                    result = pd.concat([result,chunk],axis=0)
            result.to_csv(os.path.join(self.dir,'sctri_gene_to_df_{}_{}.txt'.format(mode,key)),sep='\t',index=None)

        elif raw:
            self.uns['{}'.format(mode)][key].to_csv(os.path.join(self.dir,'sctri_gene_to_df_{}_{}.txt'.format(mode,key)),sep='\t')

    def confusion_to_df(self,mode,key):
        '''mode is confusion_reassign or confusion_sccaf'''
        self.uns['{}'.format(mode)][key].to_csv(os.path.join(self.dir,'sctri_confusion_to_df_{}_{}.txt'.format(mode,key)),sep='\t')

    def get_metrics_and_shapley(self,barcode,save=False):
        obs = self.adata.obs
        query = self.query
        total_metrics = self.total_metrics
        row = obs.loc[barcode,:]
        metrics_cols = [j + '@' + i for i in query for j in total_metrics]
        shapley_cols = [i + '_' + 'shapley' for i in query]
        row_metrics = row.loc[metrics_cols].values.reshape(len(query),len(total_metrics))
        df = pd.DataFrame(data=row_metrics,index=query,columns=total_metrics)
        row_shapley = row.loc[shapley_cols].values
        df['shapley'] = row_shapley
        row_cluster = row.loc[query].values
        df['cluster'] = row_cluster
        if save:
            df.to_csv(os.path.join(self.dir,'sctri_metrics_and_shapley_df_{}.txt'.format(barcode)),sep='\t')
        return df

    @staticmethod
    def salvage_run(step_to_start,last_step_file,compute_metrics_parallel=True,scale_sccaf=True,compute_shapley_parallel=True,win_fraction_cutoff=0.25,
                    reassign_abs_thresh=10,assess_pruned=True,viewer_cluster=True,viewer_cluster_keys=None,viewer_heterogeneity=True,
                    viewer_heterogeneity_keys=None):
        # before running this function, make sure previously generated file/folder are renamed, otherwise, they will be overwritten.
        if step_to_start == 'assess_pruned':
            sctri = ScTriangulate.deserialize(last_step_file)
            sctri.uns['raw_cluster_goodness'].to_csv(os.path.join(sctri.dir,'raw_cluster_goodness.txt'),sep='\t')
            sctri.add_to_invalid_by_win_fraction(percent=win_fraction_cutoff)
            sctri.pruning(method='reassign',abs_thresh=reassign_abs_thresh,remove1=True,reference=sctri.reference)
            sctri.plot_umap('pruned','category')
            if assess_pruned:
                sctri.run_single_key_assessment(key='pruned',scale_sccaf=scale_sccaf)
                sctri.serialize(name='after_pruned_assess.p')
            if viewer_cluster:
                sctri.viewer_cluster_feature_html()
                sctri.viewer_cluster_feature_figure(parallel=False,select_keys=viewer_cluster_keys)
            if viewer_heterogeneity:
                if viewer_heterogeneity_keys is None:
                    viewer_heterogeneity_keys = [sctri.reference]
                for key in viewer_heterogeneity_keys:
                    sctri.pruning(method='reassign',abs_thresh=reassign_abs_thresh,remove1=True,reference=key)
                    sctri.viewer_heterogeneity_html(key=key)
                    sctri.viewer_heterogeneity_figure(key=key)
            

    def lazy_run(self,compute_metrics_parallel=True,scale_sccaf=True,compute_shapley_parallel=True,win_fraction_cutoff=0.25,reassign_abs_thresh=10,
                 assess_pruned=True,viewer_cluster=True,viewer_cluster_keys=None,viewer_heterogeneity=True,viewer_heterogeneity_keys=None):
        self.compute_metrics(parallel=compute_metrics_parallel,scale_sccaf=scale_sccaf)
        self.serialize(name='after_metrics.p')
        self.compute_shapley(parallel=compute_shapley_parallel)
        self.serialize(name='after_shapley.p')
        self.pruning(method='rank',discard=None)
        self.serialize(name='after_rank_pruning.p')
        self.uns['raw_cluster_goodness'].to_csv(os.path.join(self.dir,'raw_cluster_goodness.txt'),sep='\t')
        self.add_to_invalid_by_win_fraction(percent=win_fraction_cutoff)
        self.pruning(method='reassign',abs_thresh=reassign_abs_thresh,remove1=True,reference=self.reference)
        for col in ['final_annotation','raw','pruned']:
            self.plot_umap(col,'category')
        if assess_pruned:
            self.run_single_key_assessment(key='pruned',scale_sccaf=scale_sccaf)
            self.serialize(name='after_pruned_assess.p')
        if viewer_cluster:
            self.viewer_cluster_feature_html()
            self.viewer_cluster_feature_figure(parallel=False,select_keys=viewer_cluster_keys)
        if viewer_heterogeneity:
            if viewer_heterogeneity_keys is None:
                viewer_heterogeneity_keys = [self.reference]
            for key in viewer_heterogeneity_keys:
                self.pruning(method='reassign',abs_thresh=reassign_abs_thresh,remove1=True,reference=key)
                self.viewer_heterogeneity_html(key=key)
                self.viewer_heterogeneity_figure(key=key)

            

    def add_to_invalid(self,invalid):
        try:
            self.invalid.extend(invalid)
        except AttributeError:
            self.invalid = []
            self.invalid.extend(invalid)
        finally:
            tmp = list(set(self.invalid))
            self.invalid = tmp

    def add_to_invalid_by_win_fraction(self,percent=0.25):
        df = self.uns['raw_cluster_goodness']
        invalid = df.loc[df['win_fraction']<percent,:].index.tolist()
        self.add_to_invalid(invalid)

    def clear_invalid(self):
        del self.invalid
        self.invaild = []

    def serialize(self,name='sctri_pickle.p'):
        with open(os.path.join(self.dir,name),'wb') as f:
            pickle.dump(self,f)

    @staticmethod
    def deserialize(name):
        with open(name,'rb') as f:
            sctri = pickle.load(f)
        sctri._set_logging()
        logger_sctriangulate.info('unpickled {} to memory'.format(name))
        return sctri

    def add_new_metrics(self,add_metrics):
        for metric,func in add_metrics.items():
            self.add_metrics[metric] = func
        self.total_metrics.extend(list(self.add_metrics.keys()))

    def plot_winners_statistics(self,col,plot=True,save=True):
        new_size_dict = {}  # {gs@ERP4: 100}
        for key,value in self.size_dict.items():
            for sub_key,sub_value in value.items():
                composite_key = key + '@' + sub_key
                composite_value = sub_value
                new_size_dict[composite_key] = composite_value
        obs = self.adata.obs
        winners = obs[col]
        winners_vc = winners.value_counts()
        winners_size = winners_vc.index.to_series().map(new_size_dict)
        winners_prop = winners_vc / winners_size
        winners_stats = pd.concat([winners_vc,winners_size,winners_prop],axis=1)
        winners_stats.columns = ['counts','size','proportion']
        winners_stats.sort_values(by='proportion',inplace=True)
        if plot:
            a = winners_stats['proportion']
            fig,ax = plt.subplots()
            ax.barh(y=np.arange(len(a)),width=[item for item in a.values],color='#FF9A91')
            ax.set_yticks(np.arange(len(a)))
            ax.set_yticklabels([item for item in a.index],fontsize=3)
            ax.set_title('Winners statistics')
            ax.set_xlabel('proportion of clusters that win')
            if save:
                plt.savefig(os.path.join(self.dir,'winners_statistics.pdf'),bbox_inches='tight')
                plt.close()
        return winners_stats

    def plot_clusterability(self,key,col,plot=True,save=True):
        bucket = {}   # {ERP4:5}
        obs = self.adata.obs
        for ref,grouped_df in obs.groupby(by=key):
            unique = grouped_df[col].unique()
            bucket[ref] = len(unique)
        bucket = {k: v for k, v in sorted(bucket.items(), key=lambda x: x[1])}
        if plot:
            fig,ax = plt.subplots()
            ax.scatter(x=np.arange(len(bucket)),y=list(bucket.values()),c=pick_n_colors(len(bucket)),s=100)
            ax.set_xticks(np.arange(len(bucket)))
            ax.set_xticklabels(list(bucket.keys()),fontsize=3,rotation=90)
            ax.set_title('{} clusterablity'.format(self.reference))
            ax.set_ylabel('clusterability: # sub-clusters')
            ax.spines['right'].set_visible(False)
            ax.spines['top'].set_visible(False)
            ax.grid(color='grey',alpha=0.2)
            for i in range(len(bucket)):
                ax.text(x=i,y=list(bucket.values())[i]+1,s=list(bucket.keys())[i],ha='center',va='bottom')

            if save:
                plt.savefig(os.path.join(self.dir,'{}_clusterability.pdf'.format(self.reference)),bbox_inches='tight')
                plt.close()
        return bucket


    def display_hierarchy(self,col,save=True):
        obs = self.adata.obs
        root = Node(self.reference)
        hold_ref_var = {}
        for ref,grouped_df in obs.groupby(by=self.reference):
            hold_ref_var[ref] = Node(ref,parent=root)
            unique = grouped_df[col].unique()
            if len(unique) == 1: # no sub-clusters
                continue
            else:
                hold_cluster_var = {}
                for item in unique:
                    hold_cluster_var[item] = Node(item,parent=hold_ref_var[ref])
        if save:
            with open(os.path.join(self.dir,'display_hierarchy_{}_{}.txt'.format(self.reference,col)),'a') as f:
                for pre, fill, node in RenderTree(root):
                    print("%s%s" % (pre, node.name),file=f)
        else:
            for pre, fill, node in RenderTree(root):
                print("%s%s" % (pre, node.name))


    def prune_statistics(self,print=False):
        obs = self.adata.obs
        raw = obs['raw']
        pruned = obs['pruned']
        raw_vc = raw.value_counts()
        pruned_vc = pruned.value_counts()
        pruned_vc_dict = pruned_vc.to_dict()
        tmp = raw_vc.index.map(pruned_vc_dict).fillna(value=0)
        stats_df = raw_vc.to_frame()
        stats_df['pruned'] = tmp.values
        stats_df.sort_values(by='pruned',inplace=True,ascending=False)
        self.prune_stats = stats_df
        if print:
            self.prune_stats.to_csv(os.path.join(self.dir,'sctri_prune_statistics.txt'),sep='\t')


    def doublet_predict(self):
        if issparse(self.adata.X):
            self._to_dense()
        counts_matrix = self.adata.X
        logger_sctriangulate.info('running Scrublet may take several minutes')
        scrub = scr.Scrublet(counts_matrix)
        doublet_scores,predicted_doublets = scrub.scrub_doublets(min_counts=1,min_cells=1)
        self.adata.obs['doublet_scores'] = doublet_scores
        del counts_matrix
        del scrub
  

    def _add_to_uns(self,name,key,collect):
        try:
            self.uns[name][key] = collect[name]
        except KeyError:
            self.uns[name] = {}
            self.uns[name][key] = collect[name]



    def compute_metrics(self,parallel=True,scale_sccaf=True):
        if parallel:
            cores1 = len(self.query)  # make sure to request same numeber of cores as the length of query list
            cores2 = mp.cpu_count()
            cores = min(cores1,cores2)
            logger_sctriangulate.info('Spawn to {} processes'.format(cores))
            pool = mp.Pool(processes=cores)
            self._to_sparse()
            raw_results = [pool.apply_async(each_key_run,args=(self,key,scale_sccaf,)) for key in self.query]
            pool.close()
            pool.join()
            for collect in raw_results:
                collect = collect.get()
                key = collect['key']
                for metric in self.total_metrics:
                    self.adata.obs['{}@{}'.format(metric,key)] = collect['col_{}'.format(metric)]
                self.score[key] = collect['score_info']
                self.cluster[key] = collect['cluster_info']  
                self._add_to_uns('confusion_reassign',key,collect)
                self._add_to_uns('confusion_sccaf',key,collect)
                self._add_to_uns('marker_genes',key,collect)
                self._add_to_uns('exclusive_genes',key,collect)
            subprocess.run(['rm','-r','{}'.format(os.path.join(self.dir,'scTriangulate_local_mode_enrichr/'))])
            self._to_sparse()

        else:
            logger_sctriangulate.info('choosing to compute metrics sequentially')
            for key in self.query:
                collect = each_key_run(self,key,scale_sccaf)
                key = collect['key']
                for metric in self.metrics + list(self.add_metrics.keys()):
                    self.adata.obs['{}@{}'.format(metric,key)] = collect['col_{}'.format(metric)]
                self.score[key] = collect['score_info']
                self.cluster[key] = collect['cluster_info']  
                self._add_to_uns('confusion_reassign',key,collect)
                self._add_to_uns('confusion_sccaf',key,collect)
                self._add_to_uns('marker_genes',key,collect)
                self._add_to_uns('exclusive_genes',key,collect)
            subprocess.run(['rm','-r','{}'.format(os.path.join(self.dir,'scTriangulate_local_mode_enrichr/'))])
            self._to_sparse()

    def run_single_key_assessment(self,key,scale_sccaf):
        collect = each_key_run(self,key,scale_sccaf)
        self._to_sparse()
        self.process_collect_object(collect)

    def process_collect_object(self,collect):
        key = collect['key']
        for metric in self.total_metrics:
            self.adata.obs['{}@{}'.format(metric,key)] = collect['col_{}'.format(metric)]
            self.score[key] = collect['score_info']
            self.cluster[key] = collect['cluster_info']  
            self._add_to_uns('confusion_reassign',key,collect)
            self._add_to_uns('confusion_sccaf',key,collect)
            self._add_to_uns('marker_genes',key,collect)
            self._add_to_uns('exclusive_genes',key,collect)

            

    def penalize_artifact(self,mode,stamps=None,parallel=True):
        '''void mode is to set stamp position to 0, stamp is like {leiden1:5}'''
        if mode == 'void':
            obs = self.adata.obs
            self.add_to_invalid(stamps)
            if parallel:
                obs_index = np.arange(obs.shape[0])  # [0,1,2,.....]
                cores = mp.cpu_count()
                sub_indices = np.array_split(obs_index,cores)  # indices for each chunk [(0,1,2...),(56,57,58...),(),....]
                sub_obs = [obs.iloc[sub_index,:] for sub_index in sub_indices]  # [sub_df,sub_df,...]
                pool = mp.Pool(processes=cores)
                logger_sctriangulate.info('spawn {} sub processes for penalizing artifact with mode-{}'.format(cores,mode))
                r = [pool.apply_async(func=penalize_artifact_void,args=(chunk,self.query,stamps,self.total_metrics,)) for chunk in sub_obs]
                pool.close()
                pool.join()
                results = []
                for collect in r:
                    result = collect.get()  # [sub_obs,sub_obs...]
                    results.append(result)
                obs = pd.concat(results)
                self.adata.obs = obs
            else:
                result = penalize_artifact_void(obs,self.query,stamps,self.total_metrics)
                self.adata.obs = result

        elif mode == 'cellcycle':
            # all the clusters that have cell-cycle enrichment > 0 will be collected into stamps
            marker_genes = self.uns['marker_genes']
            stamps = []
            for key,clusters in self.cluster.items():
                for cluster in clusters:
                    gsea_score = marker_genes[key].loc[cluster,:]['gsea']['cellcycle'][0]
                    gsea_hits = marker_genes[key].loc[cluster,:]['gsea']['cellcycle'][1]
                    if gsea_hits > 5 and gsea_score > 0.8:
                        stamps.append(key+'@'+cluster)
            logger_sctriangulate.info('stamps are: {}'.format(str(stamps)))
            self.invalid.extend(stamps)
            obs = self.adata.obs
            if parallel:
                obs_index = np.arange(obs.shape[0])  # [0,1,2,.....]
                cores = mp.cpu_count()
                sub_indices = np.array_split(obs_index,cores)  # indices for each chunk [(0,1,2...),(56,57,58...),(),....]
                sub_obs = [obs.iloc[sub_index,:] for sub_index in sub_indices]  # [sub_df,sub_df,...]
                pool = mp.Pool(processes=cores)
                logger_sctriangulate.info('spawn {} sub processes for penalizing artifact with mode-{}'.format(cores,mode))
                r = [pool.apply_async(func=penalize_artifact_void,args=(chunk,self.query,stamps,self.total_metrics,)) for chunk in sub_obs]
                pool.close()
                pool.join()
                results = []
                for collect in r:
                    result = collect.get()  # [sub_obs,sub_obs...]
                    results.append(result)
                obs = pd.concat(results)
                self.adata.obs = obs
            else:
                result = penalize_artifact_void(obs,self.query,stamps,self.total_metrics)
                self.adata.obs = result
            

    def regress_out_size_effect(self,regressor='background_zscore'):
        sctri = self
        '''
        the logic of this function is:
        1, take the score slot of sctriangulate object, reformat to {score:[df_a1,df_a2...],},each df_a is index(c_name),metric,size
        2. for each score, concated df will be subjected to regress_size main function, replace metric in place, deal with NA as well
        3. restore to original score slot {annotation:{score1:{value_dict}}}
        4. map back to each metric column in adata.obs
        '''
        result = {}
        order_of_keys = list(sctri.score.keys())
        for key in sctri.score.keys():
            size = get_size_in_metrics(sctri.adata.obs,key)
            slot = sctri.score[key]
            for score in slot.keys():
                df = pd.concat([pd.Series(slot[score]),pd.Series(size)],axis=1)
                try:
                    result[score].append(df)
                except KeyError:
                    result[score] = []
                    result[score].append(df)

        restore_score = {}
        for key,value in result.items():
            df_inspect_have_na = pd.concat(value,axis=0)
            df_inspect_have_na['ori'] = np.arange(df_inspect_have_na.shape[0])
            mask = df_inspect_have_na[0].isna()
            df_inspect = df_inspect_have_na.dropna(axis=0) # metric, size, ori, index is the cluster names
            df_na = df_inspect_have_na.loc[mask,:] # metric, size, ori, index is the cluster names
            df_inspect[0] = regress_size(df_inspect,regressor=regressor).values # change the metric col to regressed one
            df_na[0] = df_inspect[0].values.min() - 1 # make sure the na has smaller value than non-na ones
            df_all = pd.concat([df_inspect,df_na]).sort_values(by='ori')  # concat and reorder to the original order
            # now need to split it up, back to each annotation df
            rowptr = 0
            chunk_length = [item.shape[0] for item in value]
            for chunkptr,length in enumerate(chunk_length):
                bound = (rowptr,rowptr+length)
                target_df = df_all.iloc[bound[0]:bound[1],:]
                annotation = order_of_keys[chunkptr]
                target_dict = target_df[0].to_dict()
                try:
                    restore_score[annotation][key] = target_dict
                except KeyError:
                    restore_score[annotation] = {}
                    restore_score[annotation][key] = target_dict
                rowptr = bound[1]
        sctri.score = restore_score
        # map all back
        for key in sctri.score.keys():
            for metric in sctri.total_metrics:
                sctri.adata.obs['{}@{}'.format(metric,key)] = sctri.adata.obs[key].map(sctri.score[key]['cluster_to_{}'.format(metric)]).fillna(0).values
        
        return df_inspect_have_na,df_all



    def compute_shapley(self,parallel=True):
        if parallel:
            # compute shaley value
            score_colname = copy.deepcopy(self.total_metrics)
            score_colname.remove('doublet')
            data = np.empty([len(self.query),self.adata.obs.shape[0],len(score_colname)])  # store the metric data for each cell
            '''
            data:
            depth is how many sets of annotations
            height is how many cells
            width is how many score metrics
            '''
            for i,key in enumerate(self.query):
                practical_colname = [name + '@' + key for name in score_colname]
                data[i,:,:] = self.adata.obs[practical_colname].values
            final = []
            intermediate = []
            cores = mp.cpu_count()
            # split the obs and data, based on cell axis
            obs = self.adata.obs
            obs_index = np.arange(obs.shape[0])
            sub_indices = np.array_split(obs_index,cores)
            sub_obs = [obs.iloc[sub_index,:] for sub_index in sub_indices]  # [sub_obs, sub_obs, sub_obs]
            sub_datas = [data[:,sub_index,:] for sub_index in sub_indices]  # [sub_data,sub_data,....]
            pool = mp.Pool(processes=cores)
            logger_sctriangulate.info('spawn {} sub processes for shapley computing'.format(cores))
            raw_results = [pool.apply_async(func=run_shapley,args=(sub_obs[i],self.query,self.reference,self.size_dict,sub_datas[i])) for i in range(len(sub_obs))]
            pool.close()
            pool.join()
            for collect in raw_results: # [(final,intermediate), (), ()...]
                collect = collect.get()
                final.extend(collect[0])
                intermediate.extend(collect[1])
            self.adata.obs['final_annotation'] = final
            decisions = list(zip(*intermediate))
            for i,d in enumerate(decisions):
                self.adata.obs['{}_shapley'.format(self.query[i])] = d

            # get raw sctriangulate result
            obs = self.adata.obs
            obs_index = np.arange(obs.shape[0])  # [0,1,2,.....]
            cores = mp.cpu_count()
            sub_indices = np.array_split(obs_index,cores)  # indices for each chunk [(0,1,2...),(56,57,58...),(),....]
            sub_obs = [obs.iloc[sub_index,:] for sub_index in sub_indices]  # [sub_df,sub_df,...]
            pool = mp.Pool(processes=cores)
            logger_sctriangulate.info('spawn {} sub processes for getting raw sctriangulate result'.format(cores))
            r = pool.map_async(run_assign,sub_obs)
            pool.close()
            pool.join()
            results = r.get()  # [sub_obs,sub_obs...]
            obs = pd.concat(results)
            self.adata.obs = obs

            # prefixing
            self._prefixing(col='raw')

        else:
            # compute shaley value
            score_colname = copy.deepcopy(self.total_metrics)
            score_colname.remove('doublet')
            data = np.empty([len(self.query),self.adata.obs.shape[0],len(score_colname)])  # store the metric data for each cell
            '''
            data:
            depth is how many sets of annotations
            height is how many cells
            width is how many score metrics
            '''
            for i,key in enumerate(self.query):
                practical_colname = [name + '@' + key for name in score_colname]
                data[i,:,:] = self.adata.obs[practical_colname].values
            final = []
            intermediate = []

            # computing
            obs = self.adata.obs
            collect = run_shapley(obs,self.query,self.reference,self.size_dict,data)
            final.extend(collect[0])
            intermediate.extend(collect[1])
            self.adata.obs['final_annotation'] = final
            decisions = list(zip(*intermediate))
            for i,d in enumerate(decisions):
                self.adata.obs['{}_shapley'.format(self.query[i])] = d


            # get raw sctriangulate result
            obs = self.adata.obs
            obs = run_assign(obs)
            self.adata.obs = obs

            # prefixing
            self._prefixing(col='raw')




    def _prefixing(self,col):
        col1 = self.adata.obs[col]
        col2 = self.adata.obs[self.reference]
        col = []
        for i in range(len(col1)):
            concat = self.reference + '@' + col2[i] + '|' + col1[i]
            col.append(concat)
        self.adata.obs['prefixed'] = col


    def pruning(self,method='reassign',discard=None,scale_sccaf=True,abs_thresh=10,remove1=True,reference=None,parallel=True):
        if parallel:
            if method == 'reference':
                obs = reference_pruning(self.adata.obs,self.reference,self.size_dict)
                self.adata.obs = obs

            elif method == 'reassign':
                obs, invalid = reassign_pruning(self,abs_thresh=abs_thresh,remove1=remove1,reference=reference)
                self.adata.obs = obs
                self.invalid = invalid

            elif method == 'rank':
                obs, df = rank_pruning(self,discard=discard,scale_sccaf=scale_sccaf)
                self.adata.obs = obs
                self.uns['raw_cluster_goodness'] = df
                self.adata.obs['confidence'] = self.adata.obs['pruned'].map(df['win_fraction'].to_dict())

        self._prefixing(col='pruned')

        # finally, generate a celltype sheet
        obs = self.adata.obs
        with open(os.path.join(self.dir,'celltype.txt'),'w') as f:
            f.write('reference\tcell_cluster\tchoice\n')
            for ref,grouped_df in obs.groupby(by=self.reference):
                unique = grouped_df['pruned'].unique()
                for reassign in unique:
                    f.write('{}\t{}\n'.format(self.reference + '@' + ref,reassign))



    def get_cluster(self):
        sheet = pd.read_csv(os.path.join(self.dir,'celltype.txt'),sep='\t')
        mapping = {}
        for ref,sub_df in sheet.groupby(by='reference'):
            for cho,subsub_df in sub_df.groupby(by='choice'):
                tmp_list = subsub_df['cell_cluster'].tolist()
                composite_name = ref + '|' + '+'.join(tmp_list)
                for item in tmp_list:
                    original_name = ref + '|' + item
                    mapping[original_name] = composite_name
        self.adata.obs['user_choice'] = self.adata.obs['prefixed'].map(mapping).values
        

    def plot_umap(self,col,kind='category',save=True,format='pdf',umap_dot_size=None,umap_cmap='YlOrRd',frameon=False):
        # col means which column in obs to draw umap on
        if umap_dot_size is None:
            dot_size = 120000/self.adata.obs.shape[0]
        else:
            dot_size = umap_dot_size
        if kind == 'category':
            fig,ax = plt.subplots(nrows=2,ncols=1,figsize=(8,20),gridspec_kw={'hspace':0.3})  # for final_annotation
            sc.pl.umap(self.adata,color=col,frameon=frameon,ax=ax[0],size=dot_size)
            sc.pl.umap(self.adata,color=col,frameon=frameon,legend_loc='on data',legend_fontsize=5,ax=ax[1],size=dot_size)
            if save:
                plt.savefig(os.path.join(self.dir,'umap_sctriangulate_{}.{}'.format(col,format)),bbox_inches='tight')
                plt.close()
        elif kind == 'continuous':
            sc.pl.umap(self.adata,color=col,frameon=frameon,cmap=bg_greyed_cmap(umap_cmap),vmin=1e-5,size=dot_size)
            if save:
                plt.savefig(os.path.join(self.dir,'umap_sctriangulate_{}.{}'.format(col,format)),bbox_inches='tight')
                plt.close()

    def plot_confusion(self,name,key,save=True,format='pdf',cmap=scphere_cmap,**kwargs):
        df = self.uns[name][key]
        df = df.apply(func=lambda x:x/x.sum(),axis=1)
        sns.heatmap(df,cmap=cmap,**kwargs)  
        if save:
            plt.savefig(os.path.join(self.dir,'confusion_{}_{}.{}'.format(name,key,format)),bbox_inches='tight')
            plt.close()
    
    def plot_cluster_feature(self,key,cluster,feature,enrichment_type='enrichr',save=True,format='pdf'):
        if feature == 'enrichment':
            fig,ax = plt.subplots()
            a = self.uns['marker_genes'][key].loc[cluster,:][enrichment_type]
            ax.barh(y=np.arange(len(a)),width=[item for item in a.values()],color='#FF9A91')
            ax.set_yticks(np.arange(len(a)))
            ax.set_yticklabels([item for item in a.keys()])
            ax.set_title('Marker gene enrichment')
            ax.set_xlabel('-Log10(adjusted_pval)')
            if save:
                plt.savefig(os.path.join(self.dir,'{0}_{1}_enrichment.{2}'.format(key,cluster,format)),bbox_inches='tight')
                plt.close()
        elif feature == 'marker_genes':
            a = self.uns['marker_genes'][key].loc[cluster,:]['purify']
            top = a[:10]
            # change cmap a bit
            sc.pl.umap(self.adata,color=top,ncols=5,cmap=bg_greyed_cmap('viridis'),vmin=1e-5)
            if save:
                plt.savefig(os.path.join(self.dir,'{0}_{1}_marker_umap.{2}'.format(key,cluster,format)),bbox_inches='tight')
                plt.close()
        elif feature == 'exclusive_genes':
            a = self.uns['exclusive_genes'][key][cluster]  # self.uns['exclusive_genes'][key] is a pd.Series
            a = list(a.keys())
            top = a[:10]
            sc.pl.umap(self.adata,color=top,ncols=5,cmap=bg_greyed_cmap('viridis'),vmin=1e-5)
            if save:
                plt.savefig(os.path.join(self.dir,'{0}_{1}_exclusive_umap.{2}'.format(key,cluster,format)),bbox_inches='tight')
                plt.close()
        elif feature == 'location':
            col = [1 if item == str(cluster) else 0 for item in self.adata.obs[key]]
            self.adata.obs['tmp_plot'] = col
            sc.pl.umap(self.adata,color='tmp_plot',cmap=bg_greyed_cmap('YlOrRd'),vmin=1e-5)
            if save:
                plt.savefig(os.path.join(self.dir,'{0}_{1}_location_umap.{2}'.format(key,cluster,format)),bbox_inches='tight')
                plt.close()

    def plot_heterogeneity(self,key,cluster,style,col='pruned',save=True,format='pdf',genes=None,umap_zoom_out=True,umap_dot_size=None,
                           subset=None,marker_gene_dict=None,jitter=True,rotation=60,single_gene=None,dual_gene=None,multi_gene=None,merge=None,
                           to_sinto=False,to_samtools=False,cmap='YlOrRd',**kwarg): 
        '''
        Core plotting function in scTriangulate.

        Example::
        
            sctri.plot_heterogeneity('leiden1','0','umap',subset=['leiden1@0','leiden3@10'])
            sctri.plot_heterogeneity('leiden1','0','heatmap',subset=['leiden1@0','leiden3@10'])
            sctri.plot_heterogeneity('leiden1','0','violin',subset=['leiden1@0','leiden3@10'],genes=['MAPK14','ANXA1'])
            sctri.plot_heterogeneity('leiden1','0','sankey')
            sctri.plot_heterogeneity('leiden1','0','cellxgene')
            sctri.plot_heterogeneity('leiden1','0','heatmap+umap',subset=['leiden1@0','leiden3@10'],marker_gene_dict=marker_gene_dict)
            sctri.plot_heterogeneity('leiden1','0','dual_gene',dual_gene=['MAPK14','CD52'])

        :param key: annotation key
        :param cluster: cluster name in each annotation
   
        '''
        adata_s = self.adata[self.adata.obs[key]==cluster,:].copy()
        # remove prior color stamps
        tmp = adata_s.uns
        tmp.pop('{}_colors'.format(col),None)
        adata_s.uns = tmp

        # only consider the sub-populations in subset list
        if subset is not None:
            adata_s = adata_s[adata_s.obs[col].isin(subset),:].copy()
        if merge is not None:
            # if merge is not None, merge the sub-populations that are in each list
            # and make sure it execucate after subetting, so don't contain sub-populations that not in subset.
            # merge argument should be a nested list [('leiden1@3','leiden2@3'),('leiden3@4','leiden4@5')]
            the_map = {}
            # first put all sub_pop that needs to be concated in the map
            for need_merge in merge:
                new_concat_name = '+'.join(need_merge)
                for sub_pop in need_merge:
                    the_map[sub_pop] = new_concat_name
            # then check the remaining pop that doesn't neee to be concated, put into the_map
            all_pop = adata_s.obs[col].unique()
            remain_pop = [item for item in all_pop if item not in the_map.keys()]
            for item in remain_pop:
                the_map[item] = item
            # now map and get new column, and modifiy it back to "col"
            tmp_new_col = adata_s.obs[col].map(the_map).values
            adata_s.obs[col] = tmp_new_col

        if style == 'build':  # draw umap and heatmap

            # umap
            fig,axes = plt.subplots(nrows=2,ncols=1,gridspec_kw={'hspace':0.5},figsize=(5,10))
            # ax1
            sc.pl.umap(adata_s,color=[col],ax=axes[0])
            # ax2
            tmp_col = [1 if item == str(cluster) else 0 for item in self.adata.obs[key]]
            self.adata.obs['tmp_plot'] = tmp_col
            sc.pl.umap(self.adata,color='tmp_plot',cmap=bg_greyed_cmap(cmap),vmin=1e-5,ax=axes[1])
            if save:
                plt.savefig(os.path.join(self.dir,'{}_{}_heterogeneity_{}_{}.{}'.format(key,cluster,col,'umap',format)),bbox_inches='tight')
                plt.close()
            self.adata.obs.drop(columns=['tmp_plot'])

            # heatmap
            tmp = adata_s.uns
            tmp.pop('rank_genes_groups',None)
            adata_s.uns = tmp

            if len(adata_s.obs[col].unique()) == 1: # it is already unique
                logger_sctriangulate.info('{0} entirely being assigned to one type, no need to do DE'.format(cluster))
                return None
            else:
                sc.tl.rank_genes_groups(adata_s,groupby=col)
                adata_s = filter_DE_genes(adata_s,self.species,self.criterion)
                number_of_groups = len(adata_s.obs[col].unique())
                genes_to_pick = 50 // number_of_groups
                sc.pl.rank_genes_groups_heatmap(adata_s,n_genes=genes_to_pick,swap_axes=True,key='rank_genes_groups_filtered')
                if save:
                    plt.savefig(os.path.join(self.dir,'{}_{}_heterogeneity_{}_{}.{}'.format(key,cluster,col,'heatmap',format)),bbox_inches='tight')
                    plt.close()

        elif style == 'single_gene':
            fig,ax = plt.subplots()
            if umap_dot_size is None:
                s = 120000/self.adata.obs.shape[0]
            else:
                s = umap_dot_size
            if umap_zoom_out:
                umap_whole = self.adata.obsm['X_umap']
                umap_x_lim = (umap_whole[:,0].min(),umap_whole[:,0].max())
                umap_y_lim = (umap_whole[:,1].min(),umap_whole[:,1].max())
                ax.set_xlim(umap_x_lim)
                ax.set_ylim(umap_y_lim)
            sc.pl.umap(adata_s,color=[single_gene],size=s,ax=ax,cmap=bg_greyed_cmap(cmap),vmin=1e-5)
            if save:
                plt.savefig(os.path.join(self.dir,'{}_{}_heterogeneity_{}_{}_{}.{}'.format(key,cluster,col,style,single_gene,format)),bbox_inches='tight')
                plt.close()


        
        elif style == 'dual_gene':
            if umap_dot_size is None:
                s = 120000/self.adata.obs.shape[0]
            else:
                s = umap_dot_size
            umap_whole = self.adata.obsm['X_umap']
            umap_x_lim = (umap_whole[:,0].min(),umap_whole[:,0].max())
            umap_y_lim = (umap_whole[:,1].min(),umap_whole[:,1].max())
            dual_gene_plot(adata_s,dual_gene[0],dual_gene[1],s=s,save=save,format=format,dir=self.dir,umap_lim=[umap_x_lim,umap_y_lim])

        elif style == 'multi_gene':
            if umap_dot_size is None:
                s = 120000/self.adata.obs.shape[0]
            else:
                s = umap_dot_size            
            umap_whole = self.adata.obsm['X_umap']
            umap_x_lim = (umap_whole[:,0].min(),umap_whole[:,0].max())
            umap_y_lim = (umap_whole[:,1].min(),umap_whole[:,1].max())
            multi_gene_plot(adata_s,multi_gene,s=s,save=save,format=format,dir=self.dir,umap_lim=[umap_x_lim,umap_y_lim])


        elif style == 'heatmap+umap':
            '''first draw umap'''
            fig,axes = plt.subplots(nrows=2,ncols=1,gridspec_kw={'hspace':0.5},figsize=(5,10))
            # ax1
            if umap_zoom_out:
                umap_whole = self.adata.obsm['X_umap']
                umap_x_lim = (umap_whole[:,0].min(),umap_whole[:,0].max())
                umap_y_lim = (umap_whole[:,1].min(),umap_whole[:,1].max())
                axes[0].set_xlim(umap_x_lim)
                axes[0].set_ylim(umap_y_lim)
            if umap_dot_size is None:
                sc.pl.umap(adata_s,color=[col],ax=axes[0],size=120000/self.adata.obs.shape[0])
            else:
                sc.pl.umap(adata_s,color=[col],ax=axes[0],size=umap_dot_size)
            
            # ax2
            if subset is None:
                tmp_col = [1 if item == str(cluster) else 0 for item in self.adata.obs[key]]
            else:
                tmp_col = []
                for i in range(self.adata.obs.shape[0]):
                    ori_cluster_label = self.adata.obs[key][i]
                    prune_cluster_label = self.adata.obs[col][i]
                    if ori_cluster_label == str(cluster) and prune_cluster_label in subset:
                        tmp_col.append(1)
                    else:
                        tmp_col.append(0)
            self.adata.obs['tmp_plot'] = tmp_col
            sc.pl.umap(self.adata,color='tmp_plot',cmap=bg_greyed_cmap(cmap),vmin=1e-5,ax=axes[1])
            if save:
                plt.savefig(os.path.join(self.dir,'{}_{}_heterogeneity_{}_{}.{}'.format(key,cluster,col,'umap',format)),bbox_inches='tight')
                plt.close()
            self.adata.obs.drop(columns=['tmp_plot']) 

            '''then draw heatmap'''  
            sc.pl.heatmap(adata_s,marker_gene_dict,groupby=col,swap_axes=True,dendrogram=True)
            if save:
                plt.savefig(os.path.join(self.dir,'{}_{}_heterogeneity_{}_{}.{}'.format(key,cluster,col,'heatmap_custom',format)),bbox_inches='tight')
                plt.close()


        elif style == 'umap':
            fig,axes = plt.subplots(nrows=2,ncols=1,gridspec_kw={'hspace':0.5},figsize=(5,10))
            # ax1
            if umap_zoom_out:
                umap_whole = self.adata.obsm['X_umap']
                umap_x_lim = (umap_whole[:,0].min(),umap_whole[:,0].max())
                umap_y_lim = (umap_whole[:,1].min(),umap_whole[:,1].max())
                axes[0].set_xlim(umap_x_lim)
                axes[0].set_ylim(umap_y_lim)
            if umap_dot_size is None:
                sc.pl.umap(adata_s,color=[col],ax=axes[0],size=120000/self.adata.obs.shape[0])
            else:
                sc.pl.umap(adata_s,color=[col],ax=axes[0],size=umap_dot_size)
            
            # ax2
            if subset is None:
                tmp_col = [1 if item == str(cluster) else 0 for item in self.adata.obs[key]]
            else:
                tmp_col = []
                for i in range(self.adata.obs.shape[0]):
                    ori_cluster_label = self.adata.obs[key][i]
                    prune_cluster_label = self.adata.obs[col][i]
                    if ori_cluster_label == str(cluster) and prune_cluster_label in subset:
                        tmp_col.append(1)
                    else:
                        tmp_col.append(0)
            self.adata.obs['tmp_plot'] = tmp_col
            sc.pl.umap(self.adata,color='tmp_plot',cmap=bg_greyed_cmap(cmap),vmin=1e-5,ax=axes[1])
            if save:
                plt.savefig(os.path.join(self.dir,'{}_{}_heterogeneity_{}_{}.{}'.format(key,cluster,col,style,format)),bbox_inches='tight')
                plt.close()
            self.adata.obs.drop(columns=['tmp_plot'])

        elif style == 'heatmap':
            tmp = adata_s.uns
            tmp.pop('rank_genes_groups',None)
            adata_s.uns = tmp
            if len(adata_s.obs[col].unique()) == 1: # it is already unique
                logger_sctriangulate.info('{0} entirely being assigned to one type, no need to do DE'.format(cluster))
                return None
            else:
                sc.tl.rank_genes_groups(adata_s,groupby=col)
                adata_s = filter_DE_genes(adata_s,self.species,self.criterion)
                number_of_groups = len(adata_s.obs[col].unique())
                genes_to_pick = 50 // number_of_groups
                sc.pl.rank_genes_groups_heatmap(adata_s,n_genes=genes_to_pick,swap_axes=True,key='rank_genes_groups_filtered')
                if save:
                    plt.savefig(os.path.join(self.dir,'{}_{}_heterogeneity_{}_{}.{}'.format(key,cluster,col,style,format)),bbox_inches='tight')
                    plt.close()
                # return scanpy marker genes for each sub-populations
                sc_marker_dict = {}  # key is subgroup, value is a df containing markers
                col_dict = {}   # key is a colname, value is a numpy record array
                colnames = ['names','scores','pvals','pvals_adj','logfoldchanges']
                for item in colnames:
                    col_dict[item] = adata_s.uns['rank_genes_groups_filtered'][item]
                for group in adata_s.obs[col].unique():
                    df = pd.DataFrame()
                    for item in colnames:
                        df[item] = col_dict[item][group]
                    df.dropna(axis=0,how='any',inplace=True)
                    df.set_index(keys='names',inplace=True)
                    sc_marker_dict[group] = df
                return sc_marker_dict

        elif style == 'heatmap_custom_gene':
            sc.pl.heatmap(adata_s,marker_gene_dict,groupby=col,swap_axes=True,dendrogram=True)
            if save:
                plt.savefig(os.path.join(self.dir,'{}_{}_heterogeneity_{}_{}.{}'.format(key,cluster,col,style,format)),bbox_inches='tight')
                plt.close()

        elif style == 'violin':
            sc.pl.violin(adata_s,genes,groupby=col,rotation=rotation,jitter=jitter)
            if save:
                genes = '_'.join(genes).replace('/','_')
                plt.savefig(os.path.join(self.dir,'{}_{}_heterogeneity_{}_{}_{}.{}'.format(key,cluster,col,genes,style,format)),bbox_inches='tight')
                plt.close()
                
        elif style == 'cellxgene':
            if save:
                adata_s.write(os.path.join(self.dir,'{}_{}_heterogeneity_{}_{}.h5ad'.format(key,cluster,col,style)))
            if to_sinto:
                if not os.path.exists(os.path.join(self.dir,'sinto')):
                    os.mkdir(os.path.join(self.dir,'sinto'))
                adata_s.obs[col].to_csv(os.path.join(self.dir,'sinto','{}_{}_heterogeneity_{}_{}_to_sinto_cells.txt'.format(key,cluster,col,style)),sep='\t',header=None)
            if to_samtools:
                if not os.path.exists(os.path.join(self.dir,'samtools')):
                    os.mkdir(os.path.join(self.dir,'samtools'))
                for key_,sub_df in adata_s.obs[col].to_frame().groupby(by=col):
                    sub_df.to_csv(os.path.join(self.dir,'samtools','{}_{}_heterogeneity_{}_{}_to_samtools_{}.txt'.format(key,cluster,col,style,key_)),sep='\t',header=None,columns=[])            
            
            # how to use to_sinto or to_samtools file for visualization in IGV (take bigwig)?
            # 1. if use to_sinto to build pseudobulk
            # <1> make sure you pip install sinto
            # <2> download whole bam file, assume barcode is in CB tag field
            # <3> run the following command:
            #     sinto filterbarcodes -b /path/to/whole_bam.bam \
            #                         -c /sinto/azimuth_CD8_TCM_heterogeneity_pruned_cellxgene_to_sinto_cells.txt \
            #                         -p 30
            # <4> for each bam file, build bam.bai, then run bamCoverage:
            #     bamCoverage -b $1.bam -o $1.bw --normalizeUsing CPM -p max -bs 1 -of bigwig

            # 2. if use to_samtools to build pseudobulk
            # <1> make sure to load samtools/1.13.0
            # <2> download whole bam file, know where the barcode is stored
            # <3> run the following command:
            #     samtools view -@ 30 -b -o subset.bam -D CB:test.txt pbmc_granulocyte_sorted_10k_atac_possorted_bam.bam
            #     samtools index resultant.bam
            #     bamCoverage -b $1.bam -o $1.bw --normalizeUsing CPM -p max -bs 1 -of bigwig            
            
            return adata_s





        elif style == 'sankey':
            try:
                import plotly.graph_objects as go
                import kaleido
            except:
                logger_sctriangulate.warning('no plotly or kaleido library, fall back to matplotlib sankey plot')
                # processing the obs
                df = pd.DataFrame()
                df['ref'] = ['ref'+':'+key+'@'+cluster for _ in range(adata_s.obs.shape[0])]   # ref:gs@ERP4
                df['query'] = [item.split('@')[0] for item in adata_s.obs[col]]  # leiden1
                df['cluster'] = [item for item in adata_s.obs[col]]  # leiden1@5
                from matplotlib.sankey import Sankey
                fig,ax = plt.subplots()
                sankey = Sankey(ax=ax,head_angle=120,shoulder=0)
                # gs to query
                info1 = {target:-sub.shape[0]/df.shape[0] for target,sub in df.groupby(by='query')}
                flows1 = [1]
                flows1.extend(list(info1.values()))
                labels1 = [df['ref'].values[0]]
                labels1.extend(list(info1.keys()))
                orientations1 = [0,0]
                orientations1.extend(np.random.choice([-1,1],size=len(info1)-1).tolist())
                print(info1,flows1,labels1,orientations1)
                sankey.add(flows=flows1,labels=labels1,trunklength=4,orientations=orientations1)
                # each query to cluster
                for target,sub in df.groupby(by='query'):
                    prior_index_connect = labels1.index(target)
                    info2 = {cluster3:-subsub.shape[0]/sub.shape[0] for cluster3,subsub in sub.groupby(by='cluster')}
                    flows2 = [-flows1[prior_index_connect]]
                    flows2.extend(list(info2.values()))
                    labels2 = [target]
                    labels2.extend(list(info2.keys()))
                    orientations2 = [0,0]
                    orientations2.extend(np.random.choice([-1,1],size=len(info2)-1).tolist())
                    print(info2,flows2,labels2,orientations2)
                    sankey.add(flows=flows2,labels=labels2,trunklength=4,orientations=orientations2,prior=0,connect=(prior_index_connect,0))
                diagrams = sankey.finish()
                # adjust the text labels
                all_text = []
                for plot in diagrams:
                    all_text.append(plot.text)
                    all_text.extend(plot.texts)
                [item.set_fontsize(2) for item in all_text]
                # from adjustText import adjust_text
                # adjust_text(all_text,arrowprops=dict(arrowstyle='->',color='orange'))
                if save:
                    plt.savefig(os.path.join(self.dir,'{}_{}_heterogeneity_{}_{}.{}'.format(key,cluster,col,style,format)),bbox_inches='tight')
                    plt.close()

            else:
                df = pd.DataFrame()
                df['ref'] = ['ref'+':'+key+'@'+cluster for _ in range(adata_s.obs.shape[0])]   # ref:gs@ERP4
                df['query'] = [item.split('@')[0] for item in adata_s.obs[col]]  # leiden1
                df['cluster'] = [item for item in adata_s.obs[col]]  # leiden1@5

                unique_ref = df['ref'].unique().tolist() # not lexicographically sorted, only one
                unique_query = df['query'].unique().tolist()  # not lexicographically sorted
                unique_cluster = df['cluster'].unique().tolist() # not lexicographically sorted

                # get node label and node color
                node_label = unique_ref + unique_query + unique_cluster
                from matplotlib import cm,colors
                node_color = pick_n_colors(len(node_label))

                # get link information [(source,target,value),(),()]    
                link = []
                for target, sub in df.groupby(by='query'):
                    link_ref2query = (sub['ref'].values[0],target,sub.shape[0])
                    link.append(link_ref2query)
                    for cluster3, subsub in sub.groupby(by='cluster'):
                        link_query2cluster = (target,cluster3,subsub.shape[0])
                        link.append(link_query2cluster)
                link_info = list(zip(*link))
                link_source = [node_label.index(item) for item in link_info[0]]
                link_target = [node_label.index(item) for item in link_info[1]]
                link_value = link_info[2]
                link_color = [node_color[i] for i in link_source]

                print(node_label,node_color,link_source,link_target,link_value,link_color)

                # start to draw using plotly and save using kaleido
                node_plotly = dict(pad = 15, thickness = 20,line = dict(color = "black", width = 0.5),label = node_label,color = node_color)
                link_plotly = dict(source=link_source,target=link_target,value=link_value,color=link_color)
                fig = go.Figure(data=[go.Sankey(node = node_plotly,link = link_plotly)])
                fig.update_layout(title_text='{}_{}_heterogeneity_{}_{}'.format(key,cluster,col,style), font_size=10)
                fig.show()
                if save:
                    fig.write_image(os.path.join(self.dir,'{}_{}_heterogeneity_{}_{}.{}'.format(key,cluster,col,style,format)))


    def plot_circular_barplot(self,key,col,save=True,format='pdf'):
        # col can be 'raw' or 'pruned'
        obs = copy.deepcopy(self.adata.obs)
        reference = key
        obs['value'] = np.full(shape=obs.shape[0], fill_value=1)
        obs = obs.loc[:, [reference, col, 'value']]
        print(obs)
        obs4plot = obs.groupby(by=[reference, col])['value'].sum().reset_index()
        print(obs.groupby(by=[reference, col])['value'])
        print(obs.groupby(by=[reference, col])['value'].sum())    
        print(obs.groupby(by=[reference, col])['value'].sum().reset_index())
        cmap = colors_for_set(obs4plot[reference].unique().tolist())
        obs4plot['color'] = obs4plot[reference].map(cmap).values


        # plot layout
        upper_limit = 100
        lower_limit = 30
        outer_label_padding = 4
        inner_label_padding = 2

        # rescale the heights
        maximum = obs4plot['value'].max()
        minimum = obs4plot['value'].min()
        heights = (upper_limit - lower_limit)/(maximum - minimum)*(obs4plot['value'].values-minimum) + lower_limit
        obs4plot['value'] = heights



        # plotting
        fig = plt.figure(figsize=(10, 10))
        ax = fig.add_subplot(111, polar=True)
        ax.axis('off')
        width = 2 * np.pi / obs4plot.shape[0]
        angles = [width * (i + 1) for i in np.arange(obs4plot.shape[0])]
        bars = ax.bar(x=angles, height=obs4plot['value'].values, width=width, bottom=lower_limit, linewidth=2,
                    edgecolor='white', color=obs4plot['color'].values)



        # labels
        ax.text(x=0,y=0,s=reference,ha='center',va='center')
        for angle, height, label, ref in zip(angles, obs4plot['value'], obs4plot[col], obs4plot[reference]):
            rotation = np.rad2deg(angle)
            alignment = ''
            if angle >= np.pi/2 and angle < 3*np.pi/2:
                alignment = 'right'
                rotation = rotation + 180
            else:
                alignment = 'left'
            ax.text(x=angle, y=lower_limit + height + outer_label_padding, s=label,ha=alignment,va='center',
                    rotation=rotation, rotation_mode='anchor')  # outer labels
            #ax.text(x=angle, y=lower_limit - inner_label_padding, s=ref, va='center')  # inner labels

        # legend
        import matplotlib.patches as mpatches
        ax.legend(handles=[mpatches.Patch(color=i) for i in cmap.values()], labels=list(cmap.keys()),
                    loc='upper left', bbox_to_anchor=(0, 0), ncol=4, frameon=False, columnspacing=10,
                    title='Reference:{}'.format(reference),borderaxespad=10)

        

        if save:
            plt.savefig(os.path.join(self.dir,'sctri_circular_barplot_{}.{}'.format(col,format)),bbox_inches='tight')
            plt.close()

    def modality_contributions(self,mode='marker_genes',key='pruned',tops=20,regex_adt=r'^AB_',regex_atac=r'^chr\d{1,2}'):
        # based on how many features make into top list to measure its contribution
        # need to choose the persepctive, default is pruned column
        # will build a three maps (ADT, ATAC, RNA), each of them {c1:0.3,..} 
        # only within modality comparison makes sense
        map_adt, map_atac, map_rna = {},{},{}
        for cluster in self.adata.obs[key].unique():
            if mode == 'marker_genes':
                features = self.uns[mode][key].loc[cluster]['purify']
                tops_features = features[:tops]
                importance = np.arange(start=tops,stop=0,step=-1)
            elif mode == 'exclusive_genes':
                features = self.uns[mode][key].loc[cluster]  # a dict
                tops_features = list(features.keys())[:tops]
                importance = list(features.values())[:tops]
            for f,i in zip(tops_features,importance):
                if re.search(pattern=regex_adt,string=f):
                    try:
                        map_adt[cluster] += i
                    except KeyError:
                        map_adt[cluster] = 0
                        map_adt[cluster] += i
                elif re.search(pattern=regex_atac,string=f):
                    try:
                        map_atac[cluster] += i
                    except KeyError:
                        map_atac[cluster] = 0
                        map_atac[cluster] += i
                else:
                    try:
                        map_rna[cluster] += i
                    except KeyError:
                        map_rna[cluster] = 0
                        map_rna[cluster] += i
        self.adata.obs[key] = self.adata.obs[key].astype('O')
        self.adata.obs['adt_contribution'] = self.adata.obs[key].map(map_adt).fillna(0).astype('int64').values
        self.adata.obs['atac_contribution'] = self.adata.obs[key].map(map_atac).fillna(0).astype('int64').values   
        self.adata.obs['rna_contribution'] = self.adata.obs[key].map(map_rna).fillna(0).astype('int64').values
        self.adata.obs[key] = self.adata.obs[key].astype('category')

        


    def plot_multi_modal_feature_rank(self,cluster,mode='marker_genes',key='pruned',tops=20,
                                    regex_adt=r'^AB_',regex_atac=r'^chr\d{1,2}',save=True,format='.pdf'):
        if mode == 'marker_genes':
            features = self.uns[mode][key].loc[cluster]['purify']
            tops_features = features[:tops]
            x = np.arange(tops)
            labels = tops_features
            importance = np.arange(start=tops,stop=0,step=-1)
        elif mode == 'exclusive_genes':
            features = self.uns[mode][key].loc[cluster]  # a dict
            tops_features = list(features.keys())[:tops]
            importance = list(features.values())[:tops]
            x = np.arange(tops)
            labels = tops_features
        colors = []
        for item in labels:
            if re.search(pattern=regex_adt,string=item):
                colors.append('blue')
            elif re.search(pattern=regex_atac,string=item):
                colors.append('#3FBF90')
            else:
                colors.append('#D56DF2')
        #print(x,labels,importance,colors)
        fig,ax = plt.subplots()
        ax.bar(x=x,height=importance,width=0.5,color=colors,edgecolor='k')
        ax.set_xticks(x)
        ax.set_xticklabels(labels)
        ax.tick_params(axis='x',labelsize=6,labelrotation=90)
        ax.set_xlabel('top features')
        ax.set_ylabel('Rank(importance)')
        ax.set_title('{}_{}_{}_{}_features'.format(mode,key,cluster,tops))
        import matplotlib.patches as mpatches
        ax.legend(handles=[mpatches.Patch(color=i) for i in ['blue','#3FBF90','#D56DF2']],labels=['ADT','ATAC','RNA'],
                    frameon=False,loc='upper left',bbox_to_anchor=(1,1))
        
        if save:
            plt.savefig(os.path.join(self.dir,'sctri_multi_modal_feature_rank_{}_{}_{}_{}.{}'.format(mode,key,cluster,tops,format)),bbox_inches='tight')
            plt.close()


                              
    def plot_multi_modal_feature_fraction(self,cluster,mode='marker_genes',key='pruned',tops=[10,20,30,50],
                                    regex_adt=r'^AB_',regex_atac=r'^chr\d{1,2}',save=True,format='pdf'):
        if mode == 'marker_genes':
            features = self.uns[mode][key].loc[cluster]['purify']
        elif mode == 'exclusive_genes':
            features = self.uns[mode][key].loc[cluster]
        data = {}
        for top in tops:
            top_rna,top_adt,top_atac = 0,0,0
            top_adt_name = []
            top_features = features[:top]
            for item in top_features:
                if re.search(pattern=regex_adt,string=item):
                    top_adt += 1
                    top_adt_name.append(item)
                elif re.search(pattern=regex_atac,string=item):
                    top_atac += 1
                else:
                    top_rna += 1
            assert top_adt + top_atac + top_rna == top
            data[top] = (top_rna,top_adt,top_atac,top_adt_name)
        # plotting
        frac_rna = []
        frac_atac = []
        adt_names = []
        for k,v in data.items():
            frac_rna.append(v[0]/k)
            frac_atac.append(v[2]/k)
            adt_names.append(v[3])
        fig = plt.figure()
        gs = mpl.gridspec.GridSpec(nrows=2, ncols=len(data), height_ratios=(0.3, 0.7), hspace=0,wspace=0)
        axes1 = [fig.add_subplot(gs[0,i]) for i in range(len(data))]
        ax2 = fig.add_subplot(gs[1, :])
        # ax2 is the stacked barplot
        width = 1/(2*len(data))
        ax2.set_xlim([0,1])
        x_coord = [1/(2*len(data)) * (i*2+1) for i in range(len(data))]
        ax2.bar(x_coord,frac_rna,width=width,align='center',bottom=0,label='RNA feature',color='#D56DF2',edgecolor='k')
        ax2.bar(x_coord,frac_atac,width=width,align='center',bottom=frac_rna,label='ATAC feature',color='#3FBF90',edgecolor='k')
        ax2.legend(frameon=False,loc='upper left',bbox_to_anchor=(1,1))
        text_lower = [(item[0]+item[1])/2 for item in zip(np.full(len(data),0),frac_rna)]
        text_upper = [item[0] + 1/2 * item[1] for item in zip(frac_rna,frac_atac)]
        for i in range(len(x_coord)):
            ax2.text(x_coord[i],text_lower[i],'{:.2f}'.format(frac_rna[i]),va='center',ha='center')
            ax2.text(x_coord[i],text_upper[i],'{:.2f}'.format(frac_atac[i]),va='center',ha='center')  
        ax2.set_xticks(x_coord)
        ax2.set_xticklabels(['top{}'.format(str(i)) for i in tops])  
        ax2.set_ylabel('RNA/ATAC fractions')
        # ax1 is the single pie chart in axes1 list
        for i,lis in enumerate(adt_names):
            n = len(lis)
            if n > 0:
                axes1[i].pie(x=[100/n for i in range(n)],labels=lis,frame=True,labeldistance=None)
                axes1[i].axis('equal')
                axes1[i].tick_params(bottom=False,left=False,labelbottom=False,labelleft=False)
            else:
                axes1[i].tick_params(bottom=False,left=False,labelbottom=False,labelleft=False)
        axes1[0].set_ylabel('ADT features')
        axes1[-1].legend(loc='lower right',bbox_to_anchor=(1,1),ncol=len(data),frameon=False)
        fig.suptitle('{}_frac_{}_{}'.format(mode,key,cluster))
        if save:
            stringy_tops = '_'.join([str(item) for item in tops])
            plt.savefig(os.path.join(self.dir,'sctri_multi_modal_feature_frac_{}_{}_{}_{}.{}'.format(mode,key,cluster,stringy_tops,format)),bbox_inches='tight')
            plt.close()


    def plot_long_heatmap(self,clusters=None,key='pruned',n_features=5,mode='marker_genes',cmap='viridis',save=True,format='pdf',figsize=(6,4.8),
                          feature_fontsize=3,cluster_fontsize=5):
        df = self.uns[mode][key]
        # get feature pool
        feature_pool = []
        for i in range(df.shape[0]):
            cluster = df.index[i]
            features = df.iloc[i]['purify'][:n_features]
            feature_pool.extend(features)
        # determine cluster order
        if clusters is None:
            clusters = df.index
        core_adata = self.adata[self.adata.obs[key].isin(clusters),feature_pool]
        core_df = pd.DataFrame(data=make_sure_mat_dense(core_adata.copy().X),
                               index=core_adata.obs_names,
                               columns=core_adata.var_names)
        core_df['label'] = core_adata.obs[key].values
        centroid_df = core_df.groupby(by='label').apply(lambda x:x.iloc[:,:-1].mean(axis=0))
        dense_distance_mat = pdist(centroid_df.values,'euclidean')
        linkage_mat = linkage(dense_distance_mat,method='ward',metric='enclidean')
        leaf_order = leaves_list(linkage_mat)
        cluster_order = [centroid_df.index[i] for i in leaf_order]
        # relationship feature-cluster and barcode-cluster, and vice-versa
        feature_cluster_df = pd.DataFrame({'feature':[],'cluster':[]})
        for i in range(df.shape[0]):
            cluster = df.index[i]
            features = df.iloc[i]['purify'][:n_features]   
            chunk = pd.DataFrame({'feature':features,'cluster':np.full(len(features),fill_value=cluster)})
            feature_cluster_df = pd.concat([feature_cluster_df,chunk],axis=0) 
        feature_to_cluster = feature_cluster_df.groupby(by='feature')['cluster'].apply(lambda x:x.values[0]).to_dict()
        cluster_to_feature = feature_cluster_df.groupby(by='cluster')['feature'].apply(lambda x:x.tolist()).to_dict()

        barcode_cluster_df = pd.DataFrame({'barcode':core_adata.obs_names.tolist(),'cluster':core_adata.obs[key]})
        barcode_to_cluster = barcode_cluster_df.groupby(by='barcode')['cluster'].apply(lambda x:x.values[0]).to_dict()
        cluster_to_barcode = barcode_cluster_df.groupby(by='cluster')['barcode'].apply(lambda x:x.tolist()).to_dict()
        # plotting
        fig = plt.figure(figsize=figsize)
        gs = mpl.gridspec.GridSpec(nrows=2,ncols=2,width_ratios=(0.97,0.03),height_ratios=(0.97,0.03),wspace=0.02,hspace=0.02)
        ax1 = fig.add_subplot(gs[0,0])  # heatmap
        ax2 = fig.add_subplot(gs[1,0])  # column cell color bars
        ax3 = fig.add_subplot(gs[0,1])  # row feature color bars
        # ax1, heatmap
        p_feature = []
        for c in cluster_order:
            p_feature.extend(cluster_to_feature[c])
        p_cell = []
        for c in cluster_order:
            p_cell.extend(cluster_to_barcode[c])
        p_adata = self.adata[p_cell,p_feature].copy()
        draw_data = make_sure_mat_dense(p_adata.X).T
        im = ax1.imshow(X=draw_data,cmap=cmap,aspect='auto',interpolation='none')
        ax1.set_xticks([])
        ax1.set_yticks(np.arange(draw_data.shape[0]))
        ax1.set_yticklabels(p_adata.var_names.tolist(),fontsize=feature_fontsize)  
        # ax2, column cell color bars
        p_adata.obs['plot_cluster'] = p_adata.obs_names.map(barcode_to_cluster)
        tmp_frac = [np.count_nonzero(p_adata.obs['plot_cluster'].values==c)/p_adata.obs.shape[0] for c in cluster_order]
        tmp_cum = np.cumsum(tmp_frac)
        x_coords = [(tmp_cum[i] - tmp_frac[i]*1/2) * p_adata.obs.shape[0] for i in range(len(cluster_order))]
        anno_to_color = colors_for_set(np.sort(p_adata.obs['plot_cluster'].unique()))
        cell_column_cbar_mat = p_adata.obs['plot_cluster'].map(anno_to_color).values.reshape(1,-1)
        cell_column_cbar_mat_rgb = hex2_to_rgb3(cell_column_cbar_mat)
        ax2.imshow(X=cell_column_cbar_mat_rgb,aspect='auto',interpolation='none')
        ax2.set_xticks(x_coords)
        ax2.set_xticklabels(cluster_order,rotation=90,fontsize=cluster_fontsize)
        ax2.set_yticks([])
        ax2.set_yticklabels([])
        # ax3, row feature color bars
        p_adata.var['plot_cluster'] = p_adata.var_names.map(feature_to_cluster)
        feature_row_cbar_mat = p_adata.var['plot_cluster'].map(anno_to_color).values.reshape(-1,1)
        feature_row_cbar_mat_rgb = hex2_to_rgb3(feature_row_cbar_mat)
        ax3.imshow(X=feature_row_cbar_mat_rgb,aspect='auto',interpolation='none')
        ax3.tick_params(bottom=False,left=False,labelbottom=False,labelleft=False)
        # add white vline
        s,e = ax1.get_xlim()
        vline_coords = tmp_cum * (e-s) + s
        print(vline_coords)
        for x in vline_coords:
            ax1.axvline(x,ymin=0,ymax=1,color='white',linewidth=0.03) 
        # colorbar
        gs.update(right=0.8)
        gs_cbar = mpl.gridspec.GridSpec(nrows=1,ncols=1,left=0.85,top=0.3)
        ax4 = fig.add_subplot(gs_cbar[0,0])
        plt.colorbar(im,cax=ax4)
    
        if save:
            plt.savefig(os.path.join(self.dir,'sctri_long_umap.pdf'),bbox_inches='tight')
        # return that can be imported to morpheus
        export = pd.DataFrame(data=draw_data,columns=p_adata.obs_names,index=p_adata.var_names)
        return export


        



    def _atomic_viewer_figure(self,key):
        for cluster in self.adata.obs[key].unique():
            try:
                self.plot_cluster_feature(key,cluster,'enrichment','enrichr',True,'png')
                self.plot_cluster_feature(key,cluster,'marker_genes','enrichr',True,'png')
                self.plot_cluster_feature(key,cluster,'exclusive_genes','enrichr',True,'png')
                self.plot_cluster_feature(key,cluster,'location','enrichr',True,'png')
            except KeyError:  # the cluster only have one cell, so not in adata_compute when calculating metrics
                continue


    def _atomic_viewer_hetero(self,key):
        for cluster in self.adata.obs[key].unique():
            self.plot_heterogeneity(key,cluster,'build',format='png')


    def viewer_cluster_feature_figure(self,parallel=False,select_keys=None,other_umap=None):
        logger_sctriangulate.info('Building viewer requires generating all the necessary figures, may take several minutes')
        # see if needs to change the umap embedding
        if other_umap is not None:
            ori_umap = self.adata.obsm['X_umap']
            self.adata.obsm['X_umap'] = other_umap
        # create a folder to store all the figures
        if not os.path.exists(os.path.join(self.dir,'figure4viewer')):
            os.mkdir(os.path.join(self.dir,'figure4viewer'))
        ori_dir = self.dir
        new_dir = os.path.join(self.dir,'figure4viewer')
        self.dir = new_dir
        # generate all the figures
        '''doublet plot'''
        self.plot_umap('doublet_scores','continuous',True,'png')
        if platform.system() == 'Linux' and parallel:    # can parallelize
            cores1 = mp.cpu_count()
            cores2 = len(self.cluster)
            cores = min(cores1,cores2)
            pool = mp.Pool(processes=cores)
            logger_sctriangulate.info('spawn {} sub processes for viewer cluster feature figure generation'.format(cores))
            raw_results = [pool.apply_async(func=self._atomic_viewer_figure,args=(key,)) for key in self.cluster.keys()]
            pool.close()
            pool.join()
        else:                               # Windows and Darwin can not parallelize if plotting
            if select_keys is None:
                for key in self.cluster.keys():
                    self._atomic_viewer_figure(key)
            else:
                for key in select_keys:
                    self._atomic_viewer_figure(key)
        # dial back the dir and umap
        self.dir = ori_dir 
        if other_umap is not None:
            self.adata.obsm['X_umap'] = ori_umap

    def viewer_cluster_feature_html(self):
        # create a folder to store all the figures
        if not os.path.exists(os.path.join(self.dir,'figure4viewer')):
            os.mkdir(os.path.join(self.dir,'figure4viewer'))
        # generate html
        with open(os.path.join(self.dir,'figure4viewer','viewer.html'),'w') as f:
            f.write(to_html(self.cluster,self.score,self.total_metrics))
        os.system('cp {} {}'.format(os.path.join(os.path.dirname(os.path.abspath(__file__)),'viewer/viewer.js'),os.path.join(self.dir,'figure4viewer')))
        os.system('cp {} {}'.format(os.path.join(os.path.dirname(os.path.abspath(__file__)),'viewer/viewer.css'),os.path.join(self.dir,'figure4viewer')))

    def viewer_heterogeneity_figure(self,key,other_umap=None):
        logger_sctriangulate.info('Building viewer requires generating all the necessary figures, may take several minutes')
        # see if needs to change umap embedding
        if other_umap is not None:
            ori_umap = self.adata.obsm['X_umap']
            self.adata.obsm['X_umap'] = other_umap            
        # create a folder to store all the figures
        if not os.path.exists(os.path.join(self.dir,'figure4viewer')):
            os.mkdir(os.path.join(self.dir,'figure4viewer'))
        else:  # if already exsiting figure4viewer folder, need to clean previous figures for the specific key
            os.system('rm {}'.format(os.path.join(self.dir,'figure4viewer','{}_*_heterogeneity_*'.format(key))))          
        ori_dir = self.dir
        new_dir = os.path.join(self.dir,'figure4viewer')
        self.dir = new_dir
        
        self._atomic_viewer_hetero(key)
        # dial back
        self.dir = ori_dir
        if other_umap is not None:
            self.adata.obsm['X_umap'] = ori_umap

    def viewer_heterogeneity_html(self,key):
        key_cluster_dict = copy.deepcopy(self.cluster)
        if key not in key_cluster_dict.keys():
            key_cluster_dict[key] = self.adata.obs[key].unique().tolist()
        with open(os.path.join(self.dir,'figure4viewer','inspection_{}.html'.format(key)),'w') as f:
            f.write(inspection_html(key_cluster_dict,key)) 
        # first copy      
        os.system('cp {} {}'.format(os.path.join(os.path.dirname(os.path.abspath(__file__)),'viewer/inspection.js'),os.path.join(self.dir,'figure4viewer')))
        os.system('cp {} {}'.format(os.path.join(os.path.dirname(os.path.abspath(__file__)),'viewer/inspection.css'),os.path.join(self.dir,'figure4viewer')))






# ancillary functions for main class
def penalize_artifact_void(obs,query,stamps,metrics):
    '''
    penalize_artifact_void core function
    '''
    for stamp in stamps:
        metrics_cols = obs.loc[:,[item2+'@'+item1 for item1 in query for item2 in metrics]]
        cluster_cols = obs.loc[:,query]
        df = cluster_cols.apply(func=lambda x:pd.Series(data=[x.name+'@'+str(item) for item in x],name=x.name),axis=0)
        df_repeat = pd.DataFrame(np.repeat(df.values,len(metrics),axis=1))
        truth = pd.DataFrame(data=(df_repeat == stamp).values,index=metrics_cols.index,columns=metrics_cols.columns)
        tmp = metrics_cols.mask(truth,0)
        obs.loc[:,[item2+'@'+item1 for item1 in query for item2 in metrics]] = tmp
    return obs



def each_key_run(sctri,key,scale_sccaf):
    folder = sctri.dir
    adata = sctri.adata
    species = sctri.species
    criterion = sctri.criterion
    metrics = sctri.metrics
    add_metrics = sctri.add_metrics
    total_metrics = sctri.total_metrics

    try:
        assert issparse(adata.X) == False
    except AssertionError:
        adata.X = adata.X.toarray()  

    # remove cluster that only have 1 cell, for DE analysis
    adata_to_compute = check_filter_single_cluster(adata,key)  

    # a dynamically named dict
    cluster_to_metric = {}
    '''marker gene'''
    marker_genes = marker_gene(adata_to_compute,key,species,criterion,folder)
    logger_sctriangulate.info('Process {}, for {}, finished marker genes finding'.format(os.getpid(),key))
    '''reassign score'''
    cluster_to_metric['cluster_to_reassign'], confusion_reassign = reassign_score(adata_to_compute,key,marker_genes)
    logger_sctriangulate.info('Process {}, for {}, finished reassign score computing'.format(os.getpid(),key))
    '''tfidf10 score'''
    cluster_to_metric['cluster_to_tfidf10'], exclusive_genes = tf_idf10_for_cluster(adata_to_compute,key,species,criterion)
    logger_sctriangulate.info('Process {}, for {}, finished tfidf score computing'.format(os.getpid(),key))
    '''SCCAF score'''
    cluster_to_metric['cluster_to_SCCAF'], confusion_sccaf = SCCAF_score(adata_to_compute,key, species, criterion,scale_sccaf)
    logger_sctriangulate.info('Process {}, for {}, finished SCCAF score computing'.format(os.getpid(),key))
    '''doublet score'''
    cluster_to_metric['cluster_to_doublet'] = doublet_compute(adata_to_compute,key)
    logger_sctriangulate.info('Process {}, for {}, finished doublet score assigning'.format(os.getpid(),key))
    '''added other scores'''
    for metric,func in add_metrics.items():
        cluster_to_metric['cluster_to_{}'.format(metric)] = func(adata_to_compute,key,species,criterion)
        logger_sctriangulate.info('Process {}, for {}, finished {} score computing'.format(os.getpid(),key,metric))


    collect = {'key':key}  # collect will be retured to main program
    '''collect all default metrics and added metrics'''
    for metric in total_metrics:
        collect['col_{}'.format(metric)] = adata.obs[key].astype('str').map(cluster_to_metric['cluster_to_{}'.format(metric)]).fillna(0).values
    '''collect score info and cluster info'''
    score_info = cluster_to_metric  # {cluster_to_reassign:{cluster1:0.45}}
    cluster_info = list(cluster_to_metric['cluster_to_reassign'].keys())  #[cluster1,cluster2,cluster3]
    collect['score_info'] = score_info
    collect['cluster_info'] = cluster_info
    '''collect uns including genes and confusion matrix'''
    collect['marker_genes'] = marker_genes
    collect['exclusive_genes'] = exclusive_genes
    collect['confusion_reassign'] = confusion_reassign
    collect['confusion_sccaf'] = confusion_sccaf

    return collect


def run_shapley(obs,query,reference,size_dict,data):
    logger_sctriangulate.info('process {} need to process {} cells for shapley computing'.format(os.getpid(),data.shape[1]))
    final = []
    intermediate = []
    for i in range(data.shape[1]):
        layer = data[:,i,:]
        result = []
        for j in range(layer.shape[0]):
            result.append(shapley_value(j,layer))
        cluster_row = obs.iloc[i].loc[query].values
        to_take = which_to_take(result,query,reference,cluster_row,size_dict)   # which annotation this cell should adopt
        final.append(to_take)    
        intermediate.append(result)
    return final,intermediate


def run_assign(obs):  
    logger_sctriangulate.info('process {} need to process {} cells for raw sctriangulte result'.format(os.getpid(),obs.shape[0]))   
    assign = []
    for i in range(obs.shape[0]):
        name = obs.iloc[i,:].loc['final_annotation']
        cluster = obs.iloc[i,:].loc[name]
        concat = name + '@' + cluster
        assign.append(concat)   
    obs['raw'] = assign
    return obs

def filter_DE_genes(adata,species,criterion):
    de_gene = pd.DataFrame.from_records(adata.uns['rank_genes_groups']['names']) #column use field name, index is none by default, so incremental int value
    artifact = set(read_artifact_genes(species,criterion).index)
    de_gene.mask(de_gene.isin(artifact),inplace=True)
    adata.uns['rank_genes_groups_filtered'] = adata.uns['rank_genes_groups'].copy()
    adata.uns['rank_genes_groups_filtered']['names'] = de_gene.to_records(index=False)
    return adata












