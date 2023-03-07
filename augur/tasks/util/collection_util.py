from __future__ import annotations
from typing import List
import time
import logging
import json
import os
from enum import Enum
import math
import numpy as np
import datetime
#from celery.result import AsyncResult
from celery import signature
from celery import group, chain, chord, signature
from sqlalchemy import or_, and_
from augur.application.logs import AugurLogger
from augur.tasks.init.celery_app import celery_app as celery
from augur.application.db.models import CollectionStatus, Repo
from augur.application.db.util import execute_session_query
from augur.application.config import AugurConfig
from augur.tasks.github.util.util import get_owner_repo
from augur.tasks.github.util.gh_graphql_entities import GitHubRepo as GitHubRepoGraphql
from augur.tasks.github.util.gh_graphql_entities import GraphQlPageCollection
from augur.tasks.github.util.github_task_session import GithubTaskManifest

# class syntax
class CollectionState(Enum):
    SUCCESS = "Success"
    PENDING = "Pending"
    ERROR = "Error"
    COLLECTING = "Collecting"
    UPDATE = "Update"
    FAILED_CLONE = "Failed Clone"

def get_enabled_phase_names_from_config(logger, session):

    config = AugurConfig(logger, session)
    phase_options = config.get_section("Task_Routine")

    #Get list of enabled phases 
    enabled_phase_names = [name for name, phase in phase_options.items() if phase == 1]

    return enabled_phase_names

#Query db for CollectionStatus records that fit the desired condition.
#Used to get CollectionStatus for differant collection hooks
def get_collection_status_repo_git_from_filter(session,filter_condition,limit):
    repo_status_list = session.query(CollectionStatus).filter(filter_condition).limit(limit).all()

    return [status.repo.repo_git for status in repo_status_list]



@celery.task
def task_failed(request,exc,traceback):

    from augur.tasks.init.celery_app import engine

    logger = logging.getLogger(task_failed.__name__)

    # log traceback to error file
    logger.error(f"Task {request.id} raised exception: {exc}\n{traceback}")
    
    with DatabaseSession(logger,engine) as session:
        core_id_match = CollectionStatus.core_task_id == request.id
        secondary_id_match = CollectionStatus.secondary_task_id == request.id
        facade_id_match = CollectionStatus.facade_task_id == request.id

        query = session.query(CollectionStatus).filter(or_(core_id_match,secondary_id_match,facade_id_match))

        print(f"chain: {request.chain}")
        #Make sure any further execution of tasks dependent on this one stops.
        try:
            #Replace the tasks queued ahead of this one in a chain with None.
            request.chain = None
        except AttributeError:
            pass #Task is not part of a chain. Normal so don't log.
        except Exception as e:
            logger.error(f"Could not mutate request chain! \n Error: {e}")
        
        try:
            collectionRecord = execute_session_query(query,'one')
        except:
            #Exit if we can't find the record.
            return
        
        if collectionRecord.core_task_id == request.id:
            # set status to Error in db
            collectionRecord.core_status = CollectionStatus.ERROR.value
            collectionRecord.core_task_id = None
        

        if collectionRecord.secondary_task_id == request.id:
            # set status to Error in db
            collectionRecord.secondary_status = CollectionStatus.ERROR.value
            collectionRecord.secondary_task_id = None
            
        
        if collectionRecord.facade_task_id == request.id:
            collectionRecord.facade_status = CollectionStatus.ERROR.value
            collectionRecord.facade_task_id = None
        
        session.commit()
    
    
@celery.task
def core_task_success(repo_git):

    from augur.tasks.init.celery_app import engine

    logger = logging.getLogger(core_task_success.__name__)

    logger.info(f"Repo '{repo_git}' succeeded through core collection")

    with DatabaseSession(logger, engine) as session:

        repo = Repo.get_by_repo_git(session, repo_git)
        if not repo:
            raise Exception(f"Task with repo_git of {repo_git} but could not be found in Repo table")

        collection_status = repo.collection_status[0]

        collection_status.core_status = CollectionState.SUCCESS.value
        collection_status.core_data_last_collected = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        collection_status.core_task_id = None

        session.commit()

@celery.task
def secondary_task_success(repo_git):

    from augur.tasks.init.celery_app import engine

    logger = logging.getLogger(secondary_task_success.__name__)

    logger.info(f"Repo '{repo_git}' succeeded through secondary collection")

    with DatabaseSession(logger, engine) as session:

        repo = Repo.get_by_repo_git(session, repo_git)
        if not repo:
            raise Exception(f"Task with repo_git of {repo_git} but could not be found in Repo table")

        collection_status = repo.collection_status[0]

        collection_status.secondary_status = CollectionState.SUCCESS.value
        collection_status.secondary_data_last_collected	 = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        collection_status.secondary_task_id = None

        session.commit()

def date_weight_factor(days_since_last_collection):
    return (days_since_last_collection ** 3) / 25


def get_repo_weight_by_issue(logger,repo_git,days_since_last_collection):


    owner,name = get_owner_repo(repo_git)

    with GithubTaskManifest(logger) as manifest:
        repo_graphql = GitHubRepoGraphql(logger, manifest.key_auth, owner, name)
        number_of_issues_and_prs = len(repo_graphql.get_issues_collection()) + len(repo_graphql.get_pull_requests_collection())
    
    return number_of_issues_and_prs - date_weight_factor(days_since_last_collection)



@celery.task
def facade_task_success(repo_git):

    from augur.tasks.init.celery_app import engine

    logger = logging.getLogger(facade_task_success.__name__)

    logger.info(f"Repo '{repo_git}' succeeded through facade task collection")

    with DatabaseSession(logger, engine) as session:

        repo = Repo.get_by_repo_git(session, repo_git)
        if not repo:
            raise Exception(f"Task with repo_git of {repo_git} but could not be found in Repo table")

        collection_status = repo.collection_status[0]

        collection_status.facade_status = CollectionState.SUCCESS.value
        collection_status.facade_data_last_collected = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        collection_status.facade_task_id = None

        session.commit()

def get_repo_weight_by_commit(logger,repo_git,days_since_last_collection):
    pass


class AugurTaskRoutine:
    """class to keep track of various groups of collection tasks as well as how they relate to one another.
    Accessible like a dict, each dict item represents a 'phase' of augur collection executed more or less in parallel.

    Attributes:
        logger (Logger): Get logger from AugurLogger
        jobs_dict (dict): Dict of data collection phases to run
        repos (List[str]): List of repo_ids to run collection on.
        collection_phases (List[str]): List of phases to run in augur collection.
        session: Database session to use
    """
    def __init__(self,session,repos: List[str]=[],collection_phases: List[str]=[]):
        self.logger = AugurLogger("data_collection_jobs").get_logger()
        #self.session = TaskSession(self.logger)
        self.jobs_dict = {}
        self.collection_phases = collection_phases
        #self.disabled_collection_tasks = disabled_collection_tasks
        self.repos = repos
        self.session = session

        #Assemble default phases
        #These will then be able to be overridden through the config.
        for phase in collection_phases:
            self.jobs_dict[phase.__name__] = phase

    #Get and set dict values that correspond to phases of collection
    def __getitem__(self,key: str) -> dict:
        """Return the collection group with the specified key.
        """
        return self.jobs_dict[key]
    
    def __setitem__(self,key: str,newJobs):
        """Create a new collection job group with the name of the key specified.
        """
        self.collection_phases.append(newJobs)
        self.jobs_dict[key] = newJobs

    def start_data_collection(self):
        """Start all task items and return.
        """
        augur_collection_list = []
        
        for repo_git in self.repos:

            repo = self.session.query(Repo).filter(Repo.repo_git == repo_git).one()
            repo_id = repo.repo_id

            augur_collection_sequence = []
            for phaseName, job in self.jobs_dict.items():
                self.logger.info(f"Queuing phase {phaseName} for repo {repo_git}")
                
                #Add the phase to the sequence in order as a celery task.
                #The preliminary task creates the larger task chain 
                augur_collection_sequence.append(job(repo_git))

            #augur_collection_sequence.append(core_task_success.si(repo_git))
            #Link all phases in a chain and send to celery
            augur_collection_chain = chain(*augur_collection_sequence)
            task_id = augur_collection_chain.apply_async(link_error=task_failed.s()).task_id

            self.logger.info(f"Setting repo_id {repo_id} to collecting for repo: {repo_git}")

            #yield the value of the task_id to the calling method so that the proper collectionStatus field can be updated
            yield repo_git, task_id
