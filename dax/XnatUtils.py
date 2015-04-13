""" XnatUtils contains useful function to interface with XNAT using Pyxnat module
The functions are divided into 4 categories:
    1) Class Specific to XNAT and Spiders:
        InterfaceTemp to create an interface with XNAT using a tempfolder
        AssessorHandler to handle assessor label string and access object
        SpiderProcessHandler to handle results at the end of any spider

    2) Methods to query XNAT database and get XNAT object :

    3) Methods to Download / Upload data to XNAT

    4) Other Methods
"""

import re
import os
import sys
import glob
import socket
import shutil
import tempfile
import collections
from datetime import datetime

from pyxnat import Interface
from lxml import etree

import redcap

from dax_settings import RESULTS_DIR

import xml.etree.cElementTree as ET

NS = {'xnat' : 'http://nrg.wustl.edu/xnat',
      'proc' : 'http://nrg.wustl.edu/proc',
      'fs'   : 'http://nrg.wustl.edu/fs',
      'xsi'  : 'http://www.w3.org/2001/XMLSchema-instance'}

### VARIABLE ###
# Status:
JOB_FAILED = 'JOB_FAILED' # the job failed on the cluster.
READY_TO_UPLOAD = 'READY_TO_UPLOAD' # Job done, waiting for the Spider to upload the results

####################################################################################
#                                    1) CLASS                                      #
####################################################################################
class InterfaceTemp(Interface):
    '''Extends the functionality of Interface
    to have a temporary cache that is removed
    when .disconnect() is called.
    '''
    def __init__(self, xnat_host, xnat_user, xnat_pass, temp_dir=None):
        if not temp_dir:
            temp_dir = tempfile.mkdtemp()
        if not os.path.exists(temp_dir):
            os.mkdir(temp_dir)
        self.temp_dir = temp_dir
        super(InterfaceTemp, self).__init__(server=xnat_host, user=xnat_user, password=xnat_pass, cachedir=temp_dir)

    def disconnect(self):
        self._exec('/data/JSESSION', method='DELETE')
        shutil.rmtree(self.temp_dir)

class AssessorHandler:
    """ Class to handle assessor label string"""
    def __init__(self, label):
        """
        The purpose of this method is to split an assessor label and parse out its associated pieces
        :param label: An assessor label of the form ProjectID-x-Subject_label-x-SessionLabel-x-ScanId-x-proctype
        :return: None
        """
        self.assessor_label = label
        if len(re.findall('-x-', label)) == 3:
            self.project_id, self.subject_label, self.session_label, self.proctype = label.split('-x-')
            self.scan_id = None
        elif len(re.findall('-x-', label)) == 4:
            self.project_id, self.subject_label, self.session_label, self.scan_id, self.proctype = label.split('-x-')
        else:
            self.assessor_label = None

    def is_valid(self):
        """return true if the assessor is a valid label"""
        return self.assessor_label != None

    def project_id(self):
        """ This method retreives the project label from self
        :return: The XNAT project label
        """
        return self.project_id

    def subject_label(self):
        """ This method retrieves the subject label from self
        :return: The XNAT subject label
        """
        return self.subject_label

    def session_label(self):
        """ This method retrieves the session label from self
        :return: The XNAT session label
        """
        return self.session_label

    def scan_id(self):
        """ This method retrieves the scan id from the assessor label
        :return: The XNAT scan ID for the assessor
        """
        return self.scan_id

    def proctype(self):
        """ This method retrieves the process type from the assessor label
        :return: The XNAT process type for the assessor
        """
        return self.proctype

    def select_assessor(self, intf):
        """ return XNAT object for the assessor
        :return: None
        """
        string_obj = '''/project/{project}/subject/{subject}/experiment/{session}/assessor/{label}'''.format(project=self.project_id, subject=self.subject_label, session=self.session_label, label=self.assessor_label)
        return intf.select(string_obj)

class SpiderProcessHandler:
    """ Handle the results of a spider """
    def __init__(self, script_name, project, subject, experiment, scan=None):
        """ initialization """
        #Variables:
        self.error = 0
        self.has_pdf = 0
        # Get the process name and the version
        if len(script_name.split('/')) > 1:
            script_name = os.path.basename(script_name)
        if script_name.endswith('.py'):
            script_name = script_name[:-3]
        if 'Spider' in script_name:
            script_name = script_name[7:]

        #ge the processname from spider
        if len(re.split("/*_v[0-9]/*", script_name)) > 1:
            self.version = script_name.split('_v')[-1]
            proctype = re.split("/*_v[0-9]/*", script_name)[0]+'_v'+self.version.split('.')[0]
        else:
            self.version = '1.0.0'
            proctype = script_name

        #Create the assessor handler
        if not scan:
            assessor_label = project+'-x-'+subject+'-x-'+experiment+'-x-'+proctype
        else:
            assessor_label = project+'-x-'+subject+'-x-'+experiment+'-x-'+scan+'-x-'+proctype
        self.assr_handler = AssessorHandler(assessor_label)

        #Create the upload directory
        self.directory = os.path.join(RESULTS_DIR, assessor_label)
        #if the folder already exists : remove it
        if not os.path.exists(self.directory):
            os.mkdir(self.directory)
        else:
            #Remove files in directories
            clean_directory(self.directory)

        print'INFO: Handling results ...'
        print'''  -Creating folder {folder} for {label}'''.format(folder=self.directory, label=assessor_label)

    def set_error(self):
        """ set the error to one """
        self.error = 1

    def file_exists(self, fpath):
        """ check if file exists """
        if not os.path.isfile(fpath.strip()):
            self.error = 1
            print '''ERROR: file {file} does not exists.'''.format(file=fpath)
            return False
        else:
            return True

    def folder_exists(self, fpath):
        """ check if folder exists """
        if not os.path.isdir(fpath.strip()):
            self.error = 1
            print '''ERROR: folder {folder} does not exists.'''.format(folder=fpath)
            return False
        else:
            return True

    def print_copying_statement(self, label, src, dest):
        """ print statement for copying data """
        print '''  -Copying {label}: {src} to {dest}'''.format(label=label, src=src, dest=dest)

    def add_pdf(self, filepath):
        """ add a file to resource pdf in the upload dir """
        if self.file_exists(filepath):
            #Check if it's a ps:
            if filepath.lower().endswith('.ps'):
                pdf_path = os.path.splitext(filepath)[0]+'.pdf'
                ps2pdf_cmd = '''ps2pdf {ps} {pdf}'''.format(ps=filepath, pdf=pdf_path)
                print '''  -Convertion {cmd} ...'''.format(cmd=ps2pdf_cmd)
                os.system(ps2pdf_cmd)
            else:
                pdf_path = filepath
            self.add_file(pdf_path, 'PDF')
            self.has_pdf = 1

    def add_snapshot(self, snapshot):
        """ add a file to resource snapshot in the upload dir """
        self.add_file(snapshot, 'SNAPSHOTS')

    def add_file(self, filepath, resource):
        """ add a file to the upload dir under the resource name """
        if self.file_exists(filepath):
            #make the resource folder
            respath = os.path.join(self.directory, resource)
            if not os.path.exists(respath):
                os.mkdir(respath)
            #mv the file
            self.print_copying_statement(resource, filepath, respath)
            shutil.copyfile(filepath, respath)
            #if it's a nii or a rec file, gzip it:
            if filepath.lower().endswith('.nii') or filepath.lower().endswith('.rec'):
                os.system('gzip '+os.path.join(respath, os.path.basename(filepath)))

    def add_folder(self, folderpath, resource_name=None):
        """ add a folder to the upload dir (with a specific name if specified) """
        if self.folder_exists(folderpath):
            if not resource_name:
                res = os.path.basename(os.path.abspath(folderpath))
            else:
                res = resource_name
            dest = os.path.join(self.directory, res)

            try:
                shutil.copytree(folderpath, dest)
                self.print_copying_statement(res, folderpath, dest)
            # Directories are the same
            except shutil.Error as excep:
                print 'Directory not copied. Error: %s' % excep
            # Any error saying that the directory doesn't exist
            except OSError as excep:
                print 'Directory not copied. Error: %s' % excep

    def setAssessorStatus(self, status):
        """ Set the status of an assessor """
        # Connection to Xnat
        try:
            xnat = get_interface()
            assessor = self.assr_handler.select_assessor(xnat)
            if assessor.exists():
                if self.assr_handler.proctype() == 'FS':
                    assessor.attrs.set('fs:fsdata/procstatus', status)
                    print '  -status set for FreeSurfer to '+str(status)
                else:
                    assessor.attrs.set('proc:genProcData/procstatus', status)
                    print '  -status set for assessor to '+str(status)
        finally:
            xnat.disconnect()

    def done(self):
        """ create the flagfile and set the assessor with the new status """
        #creating the version file to give the spider version:
        f_obj = open(os.path.join(self.directory, 'version.txt'), 'w')
        f_obj.write(self.version)
        f_obj.close()
        #Finish the folder
        if not self.error and self.has_pdf:
            print 'INFO: Job ready to be upload, error: '+ str(self.error)
            #make the flag folder
            open(os.path.join(self.directory, READY_TO_UPLOAD+'.txt'), 'w').close()
            #set status to ReadyToUpload
            self.setAssessorStatus(READY_TO_UPLOAD)
        else:
            print 'INFO: Job failed, check the outlogs, error: '+ str(self.error)
            #make the flag folder
            open(os.path.join(self.directory, JOB_FAILED+'.txt'), 'w').close()
            #set status to JOB_FAILED
            self.setAssessorStatus(JOB_FAILED)

    def clean(self, directory):
        """ clean directory if no error and pdf created """
        if self.has_pdf and not self.error:
            #Remove the data
            shutil.rmtree(directory)

####################################################################################
#                     2) Query XNAT and Access XNAT obj                            #
####################################################################################
def get_interface(host=None, user=None, pwd=None):
    """ open interface with XNAT using your log-in information """
    if user == None:
        user = os.environ['XNAT_USER']
    if pwd == None:
        pwd = os.environ['XNAT_PASS']
    if host == None:
        host = os.environ['XNAT_HOST']
    # Don't sys.exit, let callers catch KeyErrors
    return InterfaceTemp(host, user, pwd)

def list_projects(intf):
    """ list of dictionaries for project that you have access to """
    post_uri = '/REST/projects'
    projects_list = intf._get_json(post_uri)
    return projects_list

def list_project_resources(intf, projectid):
    """ list of dictionaries for the project resources """
    post_uri = '/REST/projects/'+projectid+'/resources'
    resource_list = intf._get_json(post_uri)
    return resource_list

def list_subjects(intf, projectid=None):
    """ list of dictionaries for subjects in a project """
    if projectid:
        post_uri = '/REST/projects/'+projectid+'/subjects'
    else:
        post_uri = '/REST/subjects'

    post_uri += '?columns=ID,project,label,URI,last_modified,src,handedness,gender,yob'

    subject_list = intf._get_json(post_uri)

    for s in subject_list:
        if projectid:
            # Override the project returned to be the one we queried
            s['project'] = projectid

        s['project_id'] = s['project']
        s['project_label'] = s['project']
        s['subject_id'] = s['ID']
        s['subject_label'] = s['label']
        s['last_updated'] = s['src']

    return sorted(subject_list, key=lambda k: k['subject_label'])

def list_subject_resources(intf, projectid, subjectid):
    """ list of dictionaries for the subjects resources """
    post_uri = '/REST/projects/'+projectid+'/subjects/'+subjectid+'/resources'
    resource_list = intf._get_json(post_uri)
    return resource_list

def list_experiments(intf, projectid=None, subjectid=None):
    """ list of dictionaries for sessions in a project or subject with less details than list_session"""
    if projectid and subjectid:
        post_uri = '/REST/projects/'+projectid+'/subjects/'+subjectid+'/experiments'
    elif projectid == None and subjectid == None:
        post_uri = '/REST/experiments'
    elif projectid and subjectid == None:
        post_uri = '/REST/projects/'+projectid+'/experiments'
    else:
        return None

    post_uri += '?columns=ID,URI,subject_label,subject_ID,modality,project,date,xsiType,label,xnat:subjectdata/meta/last_modified'
    experiment_list = intf._get_json(post_uri)

    for e in experiment_list:
        if projectid:
            # Override the project returned to be the one we queried and add others for convenience
            e['project'] = projectid

        e['subject_id'] = e['subject_ID']
        e['session_id'] = e['ID']
        e['session_label'] = e['label']
        e['project_id'] = e['project']
        e['project_label'] = e['project']

    return sorted(experiment_list, key=lambda k: k['session_label'])

def list_experiment_resources(intf, projectid, subjectid, experimentid):
    """ list of dictionaries for the session resources """
    post_uri = '/REST/projects/'+projectid+'/subjects/'+subjectid+'/experiments/'+experimentid+'/resources'
    resource_list = intf._get_json(post_uri)
    return resource_list

def list_sessions(intf, projectid=None, subjectid=None):
    """ list of dictionaries for sessions in one project or one subject """
    type_list = []
    full_sess_list = []

    if projectid and subjectid:
        post_uri = '/REST/projects/'+projectid+'/subjects/'+subjectid+'/experiments'
    elif projectid == None and subjectid == None:
        post_uri = '/REST/experiments'
    elif projectid and subjectid == None:
        post_uri = '/REST/projects/'+projectid+'/experiments'
    else:
        return None

    # First get a list of all experiment types
    post_uri_types = post_uri+'?columns=xsiType'
    sess_list = intf._get_json(post_uri_types)
    for sess in sess_list:
        sess_type = sess['xsiType'].lower()
        if sess_type not in type_list:
            type_list.append(sess_type)

    #Get the subjects list to get the subject ID:
    subj_list = list_subjects(intf, projectid)
    subj_id2lab = dict((subj['ID'], [subj['handedness'], subj['gender'], subj['yob']]) for subj in subj_list)

    # Get list of sessions for each type since we have to specific about last_modified field
    for sess_type in type_list:
        post_uri_type = post_uri + '?xsiType='+sess_type+'&columns=ID,URI,subject_label,subject_ID,modality,project,date,xsiType,'+sess_type+'/age,label,'+sess_type+'/meta/last_modified,'+sess_type+'/original'
        sess_list = intf._get_json(post_uri_type)

        for sess in sess_list:
            # Override the project returned to be the one we queried
            if projectid:
                sess['project'] = projectid

            sess['project_id'] = sess['project']
            sess['project_label'] = sess['project']
            sess['subject_id'] = sess['subject_ID']
            sess['session_id'] = sess['ID']
            sess['session_label'] = sess['label']
            sess['session_type'] = sess_type.split('xnat:')[1].split('session')[0].upper()
            sess['type'] = sess_type.split('xnat:')[1].split('session')[0].upper()
            sess['last_modified'] = sess[sess_type+'/meta/last_modified']
            sess['last_updated'] = sess[sess_type+'/original']
            sess['age'] = sess[sess_type+'/age']
            sess['handedness'] = subj_id2lab[sess['subject_ID']][0]
            sess['gender'] = subj_id2lab[sess['subject_ID']][1]
            sess['yob'] = subj_id2lab[sess['subject_ID']][2]

        # Add sessions of this type to full list
        full_sess_list.extend(sess_list)

    # Return list sorted by label
    return sorted(full_sess_list, key=lambda k: k['session_label'])

def list_scans(intf, projectid, subjectid, experimentid):
    """ list of dictionaries for scans in one session """
    post_uri = '/REST/projects/'+projectid+'/subjects/'+subjectid+'/experiments'
    post_uri += '?columns=ID,URI,label,subject_label,project'
    post_uri += ',xnat:imagesessiondata/scans/scan/id'
    post_uri += ',xnat:imagesessiondata/scans/scan/type'
    post_uri += ',xnat:imagesessiondata/scans/scan/quality'
    post_uri += ',xnat:imagesessiondata/scans/scan/note'
    post_uri += ',xnat:imagesessiondata/scans/scan/frames'
    post_uri += ',xnat:imagesessiondata/scans/scan/series_description'
    post_uri += ',xnat:imagesessiondata/subject_id'
    scan_list = intf._get_json(post_uri)
    new_list = []

    for s in scan_list:
        if s['ID'] == experimentid or s['label'] == experimentid:
            snew = {}
            snew['scan_id'] = s['xnat:imagesessiondata/scans/scan/id']
            snew['scan_label'] = s['xnat:imagesessiondata/scans/scan/id']
            snew['scan_quality'] = s['xnat:imagesessiondata/scans/scan/quality']
            snew['scan_note'] = s['xnat:imagesessiondata/scans/scan/note']
            snew['scan_frames'] = s['xnat:imagesessiondata/scans/scan/frames']
            snew['scan_description'] = s['xnat:imagesessiondata/scans/scan/series_description']
            snew['scan_type'] = s['xnat:imagesessiondata/scans/scan/type']
            snew['ID'] = s['xnat:imagesessiondata/scans/scan/id']
            snew['label'] = s['xnat:imagesessiondata/scans/scan/id']
            snew['quality'] = s['xnat:imagesessiondata/scans/scan/quality']
            snew['note'] = s['xnat:imagesessiondata/scans/scan/note']
            snew['frames'] = s['xnat:imagesessiondata/scans/scan/frames']
            snew['series_description'] = s['xnat:imagesessiondata/scans/scan/series_description']
            snew['type'] = s['xnat:imagesessiondata/scans/scan/type']
            snew['project_id'] = projectid
            snew['project_label'] = projectid
            snew['subject_id'] = s['xnat:imagesessiondata/subject_id']
            snew['subject_label'] = s['subject_label']
            snew['session_id'] = s['ID']
            snew['session_label'] = s['label']
            snew['session_uri'] = s['URI']
            new_list.append(snew)

    return sorted(new_list, key=lambda k: k['label'])

def list_project_scans(intf, projectid, include_shared=True):
    """ list of dictionaries for scans in a project """
    new_list = []

    #Get the sessions list to get the modality:
    session_list = list_sessions(intf, projectid)
    sess_id2mod = dict((sess['session_id'], [sess['handedness'], sess['gender'], sess['yob'], sess['age'], sess['last_modified'], sess['last_updated']]) for sess in session_list)

    post_uri = '/REST/archive/experiments'
    post_uri += '?project='+projectid
    post_uri += '&xsiType=xnat:imageSessionData'
    post_uri += '&columns=ID,URI,label,subject_label,project'
    post_uri += ',xnat:imagesessiondata/subject_id'
    post_uri += ',xnat:imagescandata/id'
    post_uri += ',xnat:imagescandata/type'
    post_uri += ',xnat:imagescandata/quality'
    post_uri += ',xnat:imagescandata/note'
    post_uri += ',xnat:imagescandata/frames'
    post_uri += ',xnat:imagescandata/series_description'
    scan_list = intf._get_json(post_uri)

    for s in scan_list:
        snew = {}
        snew['scan_id'] = s['xnat:imagescandata/id']
        snew['scan_label'] = s['xnat:imagescandata/id']
        snew['scan_quality'] = s['xnat:imagescandata/quality']
        snew['scan_note'] = s['xnat:imagescandata/note']
        snew['scan_frames'] = s['xnat:imagescandata/frames']
        snew['scan_description'] = s['xnat:imagescandata/series_description']
        snew['scan_type'] = s['xnat:imagescandata/type']
        snew['ID'] = s['xnat:imagescandata/id']
        snew['label'] = s['xnat:imagescandata/id']
        snew['quality'] = s['xnat:imagescandata/quality']
        snew['note'] = s['xnat:imagescandata/note']
        snew['frames'] = s['xnat:imagescandata/frames']
        snew['series_description'] = s['xnat:imagescandata/series_description']
        snew['type'] = s['xnat:imagescandata/type']
        snew['project_id'] = projectid
        snew['project_label'] = projectid
        snew['subject_id'] = s['xnat:imagesessiondata/subject_id']
        snew['subject_label'] = s['subject_label']
        snew['session_type'] = s['xsiType'].split('xnat:')[1].split('Session')[0].upper()
        snew['session_id'] = s['ID']
        snew['session_label'] = s['label']
        snew['session_uri'] = s['URI']
        snew['handedness'] = sess_id2mod[s['ID']][0]
        snew['gender'] = sess_id2mod[s['ID']][1]
        snew['yob'] = sess_id2mod[s['ID']][2]
        snew['age'] = sess_id2mod[s['ID']][3]
        snew['last_modified'] = sess_id2mod[s['ID']][4]
        snew['last_updated'] = sess_id2mod[s['ID']][5]
        new_list.append(snew)

    if include_shared:
        post_uri = '/REST/archive/experiments'
        post_uri += '?xnat:imagesessiondata/sharing/share/project='+projectid
        post_uri += '&xsiType=xnat:imageSessionData'
        post_uri += '&columns=ID,URI,label,subject_label,project'
        post_uri += ',xnat:imagesessiondata/subject_id'
        post_uri += ',xnat:imagescandata/id'
        post_uri += ',xnat:imagescandata/type'
        post_uri += ',xnat:imagescandata/quality'
        post_uri += ',xnat:imagescandata/note'
        post_uri += ',xnat:imagescandata/frames'
        post_uri += ',xnat:imagescandata/series_description'
        scan_list = intf._get_json(post_uri)

        for s in scan_list:
            snew = {}
            snew['scan_id'] = s['xnat:imagescandata/id']
            snew['scan_label'] = s['xnat:imagescandata/id']
            snew['scan_quality'] = s['xnat:imagescandata/quality']
            snew['scan_note'] = s['xnat:imagescandata/note']
            snew['scan_frames'] = s['xnat:imagescandata/frames']
            snew['scan_description'] = s['xnat:imagescandata/series_description']
            snew['scan_type'] = s['xnat:imagescandata/type']
            snew['ID'] = s['xnat:imagescandata/id']
            snew['label'] = s['xnat:imagescandata/id']
            snew['quality'] = s['xnat:imagescandata/quality']
            snew['note'] = s['xnat:imagescandata/note']
            snew['frames'] = s['xnat:imagescandata/frames']
            snew['series_description'] = s['xnat:imagescandata/series_description']
            snew['type'] = s['xnat:imagescandata/type']
            snew['project_id'] = projectid
            snew['project_label'] = projectid
            snew['subject_id'] = s['xnat:imagesessiondata/subject_id']
            snew['subject_label'] = s['subject_label']
            snew['session_type'] = s['xsiType'].split('xnat:')[1].split('Session')[0].upper()
            snew['session_id'] = s['ID']
            snew['session_label'] = s['label']
            snew['session_uri'] = s['URI']
            snew['handedness'] = sess_id2mod[s['ID']][0]
            snew['gender'] = sess_id2mod[s['ID']][1]
            snew['yob'] = sess_id2mod[s['ID']][2]
            snew['age'] = sess_id2mod[s['ID']][3]
            snew['last_modified'] = sess_id2mod[s['ID']][4]
            snew['last_updated'] = sess_id2mod[s['ID']][5]
            new_list.append(snew)

    return sorted(new_list, key=lambda k: k['scan_label'])

def list_scan_resources(intf, projectid, subjectid, experimentid, scanid):
    """ list of dictionaries for the scan resources """
    post_uri = '/REST/projects/'+projectid+'/subjects/'+subjectid+'/experiments/'+experimentid+'/scans/'+scanid+'/resources'
    resource_list = intf._get_json(post_uri)
    return resource_list

def list_assessors(intf, projectid, subjectid, experimentid):
    """ list of dictionaries for assessors in one session """
    new_list = []

    # First get FreeSurfer
    post_uri = '/REST/projects/'+projectid+'/subjects/'+subjectid+'/experiments/'+experimentid+'/assessors'
    post_uri += '?columns=ID,label,URI,xsiType,project,xnat:imagesessiondata/subject_id,xnat:imagesessiondata/id,xnat:imagesessiondata/label,URI,fs:fsData/procstatus,fs:fsData/validation/status&xsiType=fs:fsData'
    assessor_list = intf._get_json(post_uri)

    for a in assessor_list:
        anew = {}
        anew['ID'] = a['ID']
        anew['label'] = a['label']
        anew['uri'] = a['URI']
        anew['assessor_id'] = a['ID']
        anew['assessor_label'] = a['label']
        anew['assessor_uri'] = a['URI']
        anew['project_id'] = projectid
        anew['project_label'] = projectid
        anew['subject_id'] = a['xnat:imagesessiondata/subject_id']
        anew['session_id'] = a['session_ID']
        anew['session_label'] = a['session_label']
        anew['procstatus'] = a['fs:fsdata/procstatus']
        anew['qcstatus'] = a['fs:fsdata/validation/status']
        anew['proctype'] = 'FreeSurfer'
        anew['xsiType'] = a['xsiType']
        new_list.append(anew)

    # Then add genProcData
    post_uri = '/REST/projects/'+projectid+'/subjects/'+subjectid+'/experiments/'+experimentid+'/assessors'
    post_uri += '?columns=ID,label,URI,xsiType,project,xnat:imagesessiondata/subject_id,xnat:imagesessiondata/id,xnat:imagesessiondata/label,proc:genprocdata/procstatus,proc:genprocdata/proctype,proc:genprocdata/validation/status&xsiType=proc:genprocdata'
    assessor_list = intf._get_json(post_uri)

    for a in assessor_list:
        anew = {}
        anew['ID'] = a['ID']
        anew['label'] = a['label']
        anew['uri'] = a['URI']
        anew['assessor_id'] = a['ID']
        anew['assessor_label'] = a['label']
        anew['assessor_uri'] = a['URI']
        anew['project_id'] = projectid
        anew['project_label'] = projectid
        anew['subject_id'] = a['xnat:imagesessiondata/subject_id']
        anew['session_id'] = a['session_ID']
        anew['session_label'] = a['session_label']
        anew['procstatus'] = a['proc:genprocdata/procstatus']
        anew['proctype'] = a['proc:genprocdata/proctype']
        anew['qcstatus'] = a['proc:genprocdata/validation/status']
        anew['xsiType'] = a['xsiType']
        new_list.append(anew)

    return sorted(new_list, key=lambda k: k['label'])

def list_project_assessors(intf, projectid):
    """ list of dictionaries for assessors in a project """
    new_list = []

    #Get the sessions list to get the different variables needed:
    session_list = list_sessions(intf, projectid)
    sess_id2mod = dict((sess['session_id'], [sess['subject_label'], sess['type'], sess['handedness'], sess['gender'], sess['yob'], sess['age'], sess['last_modified'], sess['last_updated']]) for sess in session_list)

    # First get FreeSurfer
    post_uri = '/REST/archive/experiments'
    post_uri += '?project='+projectid
    post_uri += '&xsiType=fs:fsdata'
    post_uri += '&columns=ID,label,URI,xsiType,project'
    post_uri += ',xnat:imagesessiondata/subject_id,subject_label,xnat:imagesessiondata/id'
    post_uri += ',xnat:imagesessiondata/label,URI,fs:fsData/procstatus'
    post_uri += ',fs:fsData/validation/status,fs:fsData/procversion,fs:fsData/jobstartdate,fs:fsData/memused,fs:fsData/walltimeused,fs:fsData/jobid,fs:fsData/jobnode'
    assessor_list = intf._get_json(post_uri)

    for a in assessor_list:
        if a['label']:
            anew = {}
            anew['ID'] = a['ID']
            anew['label'] = a['label']
            anew['uri'] = a['URI']
            anew['assessor_id'] = a['ID']
            anew['assessor_label'] = a['label']
            anew['assessor_uri'] = a['URI']
            anew['project_id'] = projectid
            anew['project_label'] = projectid
            anew['subject_id'] = a['xnat:imagesessiondata/subject_id']
            anew['subject_label'] = a['subject_label']
            anew['session_type'] = sess_id2mod[a['session_ID']][1]
            anew['session_id'] = a['session_ID']
            anew['session_label'] = a['session_label']
            anew['procstatus'] = a['fs:fsdata/procstatus']
            anew['qcstatus'] = a['fs:fsdata/validation/status']
            anew['proctype'] = 'FreeSurfer'

            if len(a['label'].rsplit('-x-FS')) > 1:
                anew['proctype'] = anew['proctype']+a['label'].rsplit('-x-FS')[1]

            anew['version'] = a.get('fs:fsdata/procversion')
            anew['xsiType'] = a['xsiType']
            anew['jobid'] = a.get('fs:fsdata/jobid')
            anew['jobstartdate'] = a.get('fs:fsdata/jobstartdate')
            anew['memused'] = a.get('fs:fsdata/memused')
            anew['walltimeused'] = a.get('fs:fsdata/walltimeused')
            anew['jobnode'] = a.get('fs:fsdata/jobnode')
            anew['handedness'] = sess_id2mod[a['session_ID']][2]
            anew['gender'] = sess_id2mod[a['session_ID']][3]
            anew['yob'] = sess_id2mod[a['session_ID']][4]
            anew['age'] = sess_id2mod[a['session_ID']][5]
            anew['last_modified'] = sess_id2mod[a['session_ID']][6]
            anew['last_updated'] = sess_id2mod[a['session_ID']][7]
            new_list.append(anew)

    # Then add genProcData
    post_uri = '/REST/archive/experiments'
    post_uri += '?project='+projectid
    post_uri += '&xsiType=proc:genprocdata'
    post_uri += '&columns=ID,label,URI,xsiType,project'
    post_uri += ',xnat:imagesessiondata/subject_id,xnat:imagesessiondata/id'
    post_uri += ',xnat:imagesessiondata/label,proc:genprocdata/procstatus'
    post_uri += ',proc:genprocdata/proctype,proc:genprocdata/validation/status,proc:genprocdata/procversion'
    post_uri += ',proc:genprocdata/jobstartdate,proc:genprocdata/memused,proc:genprocdata/walltimeused,proc:genprocdata/jobid,proc:genprocdata/jobnode'
    assessor_list = intf._get_json(post_uri)

    for a in assessor_list:
        if a['label']:
            anew = {}
            anew['ID'] = a['ID']
            anew['label'] = a['label']
            anew['uri'] = a['URI']
            anew['assessor_id'] = a['ID']
            anew['assessor_label'] = a['label']
            anew['assessor_uri'] = a['URI']
            anew['project_id'] = projectid
            anew['project_label'] = projectid
            anew['subject_id'] = a['xnat:imagesessiondata/subject_id']
            anew['subject_label'] = sess_id2mod[a['session_ID']][0]
            anew['session_type'] = sess_id2mod[a['session_ID']][1]
            anew['session_id'] = a['session_ID']
            anew['session_label'] = a['session_label']
            anew['procstatus'] = a['proc:genprocdata/procstatus']
            anew['proctype'] = a['proc:genprocdata/proctype']
            anew['qcstatus'] = a['proc:genprocdata/validation/status']
            anew['version'] = a['proc:genprocdata/procversion']
            anew['xsiType'] = a['xsiType']
            anew['jobid'] = a.get('proc:genprocdata/jobid')
            anew['jobnode'] = a.get('proc:genprocdata/jobnode')
            anew['jobstartdate'] = a.get('proc:genprocdata/jobstartdate')
            anew['memused'] = a.get('proc:genprocdata/memused')
            anew['walltimeused'] = a.get('proc:genprocdata/walltimeused')
            anew['handedness'] = sess_id2mod[a['session_ID']][2]
            anew['gender'] = sess_id2mod[a['session_ID']][3]
            anew['yob'] = sess_id2mod[a['session_ID']][4]
            anew['age'] = sess_id2mod[a['session_ID']][5]
            anew['last_modified'] = sess_id2mod[a['session_ID']][6]
            anew['last_updated'] = sess_id2mod[a['session_ID']][7]
            new_list.append(anew)

    return sorted(new_list, key=lambda k: k['label'])

def list_assessor_out_resources(intf, projectid, subjectid, experimentid, assessorid):
    """ list of dictionaries for the assessor resources """
    post_uri = '/REST/projects/'+projectid+'/subjects/'+subjectid+'/experiments/'+experimentid+'/assessors/'+assessorid+'/out/resources'
    resource_list = intf._get_json(post_uri)
    return resource_list

def get_resource_lastdate_modified(xnat, resource):
    """ get the last modified data for a resource on XNAT (NOT WORKING: bug on XNAT side) """
    # xpaths for times in resource xml
    CREATED_XPATH = "/cat:Catalog/cat:entries/cat:entry/@createdTime"
    MODIFIED_XPATH = "/cat:Catalog/cat:entries/cat:entry/@modifiedTime"
    # Get the resource object and its uri
    res_xml_uri = resource._uri+'?format=xml'
    # Get the XML for resource
    xmlstr = xnat._exec(res_xml_uri, 'GET')
    # Parse out the times
    root = etree.fromstring(xmlstr)
    create_times = root.xpath(CREATED_XPATH, namespaces=root.nsmap)
    mod_times = root.xpath(MODIFIED_XPATH, namespaces=root.nsmap)
    # Find the most recent time
    all_times = create_times + mod_times
    if all_times:
        max_time = max(all_times)
        date = max_time.split('.')[0]
        res_date = date.split('T')[0].replace('-', '')+date.split('T')[1].replace(':', '')
    else:
        res_date = ('{:%Y-%m-%d %H:%M:%S}'.format(datetime.now())).strip().replace('-', '').replace(':', '').replace(' ', '')
    return res_date

def select_assessor(intf, assessor_label):
    """ select assessor from his label """
    labels = assessor_label.split('-x-')
    return intf.select('/project/'+labels['0']+'/subject/'+labels['1']+'/experiment/'+labels['2']+'/assessor/'+assessor_label)

def get_full_object(intf, obj_dict):
    """ select object on XNAT from dictionary """
    if 'scan_id' in obj_dict:
        proj = obj_dict['project_id']
        subj = obj_dict['subject_id']
        sess = obj_dict['session_id']
        scan = obj_dict['scan_id']
        return intf.select('/project/'+proj+'/subject/'+subj+'/experiment/'+sess+'/scan/'+scan)
    elif 'xsiType' in obj_dict and (obj_dict['xsiType'] == 'fs:fsData' or obj_dict['xsiType'] == 'proc:genProcData'):
        proj = obj_dict['project_id']
        subj = obj_dict['subject_id']
        sess = obj_dict['session_id']
        assr = obj_dict['assessor_id']
        return intf.select('/project/'+proj+'/subject/'+subj+'/experiment/'+sess+'/assessor/'+assr)
    elif 'experiments' in obj_dict['URI']:
        proj = obj_dict['project']
        subj = obj_dict['subject_ID']
        sess = obj_dict['ID']
        return intf.select('/project/'+proj+'/subject/'+subj+'/experiment/'+sess)
    elif 'subjects' in obj_dict['URI']:
        proj = obj_dict['project']
        subj = obj_dict['ID']
        return intf.select('/project/'+proj+'/subject/'+subj)
    elif 'projects' in obj_dict['URI']:
        proj = obj_dict['project']
        return intf.select('/project/'+proj)
    else:
        return intf.select('/project/')  #Return non existing object: obj.exists() -> False

def get_assessor(xnat, projid, subjid, sessid, assrid):
    """ select assessor from ids or labels """
    assessor = xnat.select('/projects/'+projid+'/subjects/'+subjid+'/experiments/'+sessid+'/assessors/'+assrid)
    return assessor

def select_obj(intf, project_id=None, subject_id=None, session_id=None, scan_id=None, assessor_id=None, resource=None):
    """ Select different level object from XNAT by giving the label or id """
    select_str = ''
    if not project_id:
        print "ERROR: select_obj in XnatUtils: can not select if no project_id given."
        return intf.select('/project/')  #Return non existing object: obj.exists() -> False
    if scan_id and assessor_id:
        print "ERROR: select_obj in XnatUtils: can not select scan_id and assessor_id at the same time."
        return intf.select('/project/')  #Return non existing object: obj.exists() -> False
    tmp_dict = collections.OrderedDict([('project', project_id), ('subject', subject_id), ('experiment', session_id), ('scan', scan_id), ('assessor', assessor_id)])
    if assessor_id:
        tmp_dict['out/resource'] = resource
    else:
        tmp_dict['resource'] = resource

    for key, value in tmp_dict.items():
        if value:
            select_str += '''/{key}/{label}'''.format(key=key, label=value)
    return intf.select(select_str)

####################################################################################
#                     Download/Upload resources from XNAT                          #
####################################################################################
def check_dl_inputs(directory, xnat_obj, fctname):
    """ Check the inputs for the download function: directory and xnat_obj """
    if not os.path.exists(directory):
        print '''ERROR: {fct} in XnatUtils: Folder {path} does not exist.'''.format(fct=fctname, path=directory)
        return False
    if not xnat_obj.exists():
        print '''ERROR: {fct} in XnatUtils: xnat object for parent <{label}> does not exist on XNAT.'''.format(fct=fctname, label=xnat_obj.parent().label())
        return False
    return True

def islist(argument, argname):
    """ check if the input is a list. If a string, convert to list, else error """
    if isinstance(argument, list):
        pass
    elif isinstance(argument, str):
        argument = [argument]
    else:
        print """ERROR: download_scantypes in XnatUtils: wrong format for {name}.""".format(name=argname)
        argument = list()
    return argument

def download_file_from_obj(directory, resource_obj, fname=None):
    """ Download file with the path fname from a resource object from XNAT
        Inputs: 
            directory: directory where the data will be downloaded
            resource_obj: resource object from XNAT for any level (project/subject/session/scan/assessor)
            fname: filepath on XNAT for the resource you want to download
                   e.g: download_file(...., fname='slicesdir/index.html')
                   if not set, download biggest file
        Return:
            return the file path on your local computer for the file downloaded
    """
    if not check_dl_inputs(directory, resource_obj, 'download_file_from_obj'):
        return None

    if fname:
        if resource_obj.file(fname).exists():
            fpath = os.path.join(directory, os.path.basename(fname))
            resource_obj.file(fname).get(fpath)
            return fpath
        else:
            print '''ERROR: download_resource in XnatUtils: file {name} does not exist for resource {label}.'''.format(name=fname, label=resource_obj.label())
            return None
    else:
        return download_biggest_file_from_obj(directory, resource_obj)

def download_file(directory, resource, project_id=None, subject_id=None, session_id=None, scan_id=None, assessor_id=None, fname=None):
    """ Download file with the path fname from a resource information (project/subject/...) from XNAT
        Inputs: 
            directory: directory where the data will be downloaded
            project_id: project ID on XNAT
            subject_id: subject ID or label on XNAT
            session_id: session ID or label on XNAT
            scan_id: scan ID on XNAT
            assessor_id: assessor ID or label on XNAT
            resource: resource name on XNAT
            fname: filepath on XNAT for the resource you want to download
                   e.g: download_file(...., fname='slicesdir/index.html')
                   if not set, download biggest file
        Return:
            return the file path on your local computer for the file downloaded
    """
    xnat = get_interface()
    resource_obj = select_obj(xnat, project_id, subject_id, session_id, scan_id, assessor_id, resource)
    fpath = download_file_from_obj(directory, resource_obj, fname)
    xnat.disconnect()
    return fpath

def download_files_from_obj(directory, resource_obj):
    """ Download all files from a resource object from XNAT
        Inputs: 
            directory: directory where the data will be downloaded
            resource_obj: resource object from XNAT for any level (project/subject/session/scan/assessor)
        Return:
            return list of filepaths on your local computer for the files downloaded
    """
    fpaths = list()
    if not check_dl_inputs(directory, resource_obj, 'download_files_from_obj'):
        return fpaths #return empty list without anything being download

    resource_obj.get(directory, extract=True)
    resource_dir = os.path.join(directory, resource_obj.label())
    for root, _, filenames in os.walk(resource_dir):
        fpaths.extend([os.path.join(root, filename) for filename in filenames])

    return fpaths

def download_files(directory, resource, project_id=None, subject_id=None, session_id=None, scan_id=None, assessor_id=None):
    """ Download all files from a resource information (project/subject/...) from XNAT 
        Inputs: 
            directory: directory where the data will be downloaded
            project_id: project ID on XNAT
            subject_id: subject ID or label on XNAT
            session_id: session ID or label on XNAT
            scan_id: scan ID on XNAT
            assessor_id: assessor ID or label on XNAT
            resource: resource name on XNAT 
        Return:
            return list of filepaths on your local computer for the files downloaded
    """
    xnat = get_interface()
    resource_obj = select_obj(xnat, project_id, subject_id, session_id, scan_id, assessor_id, resource)
    fpaths = download_files_from_obj(directory, resource_obj)
    xnat.disconnect()
    return fpaths

def download_biggest_file_from_obj(directory, resource_obj):
    """ Download biggest file from a resource object from XNAT
        Inputs: 
            directory: directory where the data will be downloaded
            resource_obj: resource object from XNAT for any level (project/subject/session/scan/assessor)
        Return:
            return filepath on your local computer for the file downloaded
    """
    file_index = 0
    biggest_size = 0
    if not check_dl_inputs(directory, resource_obj, 'download_biggest_file_from_obj'):
        return None

    for index, file_obj in enumerate(resource_obj.files()):
        fsize = int(file_obj.size())
        if biggest_size < fsize:
            biggest_size = fsize
            file_index = index
    if biggest_size > 0:
        resource_fname = resource_obj.files().get()[file_index]
        resource_obj.file(resource_fname).get(os.path.join(directory, resource_fname))
        return os.path.join(directory, resource_fname)
    else:
        return None

def download_biggest_file(directory, resource, project_id=None, subject_id=None, session_id=None, scan_id=None, assessor_id=None):
    """ Download biggest file from a resource information (project/subject/...) from XNAT
        Inputs: 
            directory: directory where the data will be downloaded
            project_id: project ID on XNAT
            subject_id: subject ID or label on XNAT
            session_id: session ID or label on XNAT
            scan_id: scan ID on XNAT
            assessor_id: assessor ID or label on XNAT
            resource: resource name on XNAT 
        Return:
            return filepath on your local computer for the file downloaded
    """
    xnat = get_interface()
    resource_obj = select_obj(xnat, project_id, subject_id, session_id, scan_id, assessor_id, resource)
    fpath = download_biggest_file_from_obj(directory, resource_obj)
    xnat.disconnect()
    return fpath

def download_from_obj(directory, xnat_obj, resources, all_files=False):
    """ Download resources from an object from XNAT (project/subject/session/scan(or)assessor)
        Inputs: 
            directory: directory where the data will be downloaded
            xnat_obj: selected object from XNAT that can have a resource (project or subject or session or scan or assessor)
            resources: list of resources name on XNAT
            all_files: download all the files from the resources. If False, download the biggest file.
        Return:
            list of files path downloaded on your local computer
    """
    fpaths = list()
    if not check_dl_inputs(directory, xnat_obj, 'download_from_obj'):
        return fpaths

    resources = islist(resources, 'resources')
    if not resources:
        return fpaths

    for resource in resources:
        if xnat_obj.datatype() in ['proc:genProcData', 'fs:fsData']:
            resource_obj = xnat_obj.out_resource(resource)
        else:
            resource_obj = xnat_obj.resource(resource)
        if all_files:
            fpath = download_files_from_obj(directory, resource_obj)
            fpaths.append(fpath)
        else:
            fpath = download_biggest_file_from_obj(directory, resource_obj)
            fpaths.append(fpath)
    return fpaths

def download(directory, resources, project_id=None, subject_id=None, session_id=None, scan_id=None, assessor_id=None, all_files=False):
    """ Download resources from information provided for an object from XNAT (project/subject/session/scan(or)assessor)
        Inputs: 
            directory: directory where the data will be downloaded
            project_id: project ID on XNAT
            subject_id: subject ID or label on XNAT
            session_id: session ID or label on XNAT
            scan_id: scan ID on XNAT
            assessor_id: assessor ID or label on XNAT
            resources: list of resources name on XNAT
            all_files: download all the files from the resources. If False, download the biggest file.
        Return:
            list of files downloaded on your local computer
    """
    xnat = get_interface()
    xnat_obj = select_obj(xnat, project_id, subject_id, session_id, scan_id, assessor_id)
    fpaths = download_from_obj(directory, xnat_obj, resources, all_files)
    xnat.disconnect()
    return fpaths

def download_scan_types(directory, project_id, subject_id, session_id, scantypes, resources, all_files=False):
    """ Download resources for a session for specific scantypes
        Inputs: 
            directory: directory where the data will be downloaded
            project_id: project ID on XNAT
            subject_id: subject ID or label on XNAT
            session_id: session ID or label on XNAT
            scantypes: list of scantypes on XNAT (e.g: ['T1','fMRI'])
            resources: list of resources name on XNAT
            all_files: download all the files from the resources. If False, download the biggest file.
        Return:
            list of files downloaded
    """
    fpaths = list()
    scantypes = islist(scantypes, 'scantypes')
    if not scantypes:
        return fpaths
    xnat = get_interface()
    for scan in list_scans(xnat, project_id, subject_id, session_id):
        if scan['type'] in scantypes:
            scan_obj = select_obj(xnat, project_id, subject_id, session_id, scan['ID'])
            fpaths.extend(download_from_obj(directory, scan_obj, resources, all_files))
    xnat.disconnect()
    return fpaths

def download_scan_seriesdescriptions(directory, project_id, subject_id, session_id, seriesdescriptions, resources, all_files=False):
    """ Download resources for a session for specific series description
        Inputs: 
            directory: directory where the data will be downloaded
            project_id: project ID on XNAT
            subject_id: subject ID or label on XNAT
            session_id: session ID or label on XNAT
            seriesdescription: list of series description on XNAT
            resources: list of resources name on XNAT
            all_files: download all the files from the resources. If False, download the biggest file.
        Return:
            list of files downloaded
    """
    fpaths = list()
    seriesdescriptions = islist(seriesdescriptions, 'seriesdescription')
    if not seriesdescriptions:
        return fpaths
    xnat = get_interface()
    for scan in list_scans(xnat, project_id, subject_id, session_id):
        if scan['series_description'] in seriesdescriptions:
            scan_obj = select_obj(xnat, project_id, subject_id, session_id, scan['ID'])
            fpaths.extend(download_from_obj(directory, scan_obj, resources, all_files))
    xnat.disconnect()
    return fpaths

def download_assessor_proctypes(directory, project_id, subject_id, session_id, proctypes, resources, all_files=False):
    """ Download resources for a session for specific assessor type (proctype)
        Inputs: 
            directory: directory where the data will be downloaded
            project_id: project ID on XNAT
            subject_id: subject ID or label on XNAT
            session_id: session ID or label on XNAT
            proctypes: list of proctypes on XNAT (e.g: ['fMRIQA_v2','dtiQA_v3'])
            resources: list of resources name on XNAT
            all_files: download all the files from the resources. If False, download the biggest file.
        Return:
            list of files downloaded
    """
    fpaths = list()
    proctypes = islist(proctypes, 'proctypes')
    if not proctypes:
        return fpaths
    proctypes = set([proctype.replace('FreeSurfer', 'FS') for proctype in proctypes])
    xnat = get_interface()
    for assessor in list_assessors(xnat, project_id, subject_id, session_id):
        if assessor['proctype'] in proctypes:
            assessor_obj = select_obj(xnat, project_id, subject_id, session_id, assessor_id=assessor['label'])
            fpaths.extend(download_from_obj(directory, assessor_obj, resources, all_files))
    xnat.disconnect()
    return fpaths

def upload_file_from_obj(filepath, resource_obj, remove=False, removeall=False, fname=None):
    """ Upload file to the resource_obj given to the function 
        Inputs: 
            filepath: path of the file on your local computer
            resource_obj: resource object on XNAT (select resource for project or subject or session or scan or assessor)
            remove: remove files that already exists on the resource.
            removeall: remove all previous files on the resource.
            fname: give a different name for the file on XNAT (e.g: target.nii.gz --> results/target.nii.gz)
                   this will create a folder results and put the file in it for the resource.
        Return:
            status of the upload (True or False)
    """
    if os.path.isfile(filepath): #Check existence of the file
        if removeall and resource_obj.exists: #Remove previous resource to upload the new one
            resource_obj.delete()
        filepath = check_image_format(filepath)
        if fname:
            filename = fname
            if filepath.endswith('.gz') and not fname.endswith('.gz'):
                filename += '.gz'
        else:
            filename = os.path.basename(filepath)
        if resource_obj.file(str(filename)).exists():
            if remove:
                resource_obj.file(str(filename)).delete()
            else:
                print """WARNING: upload_folder in XnatUtils: resource {filename} already exists.""".format(filename=filename)
                return False
        resource_obj.file(str(filename)).put(str(filepath))
        return True
    else:
        print """ERROR: upload_folder in XnatUtils: file {file} doesn't exist.""".format(file=filepath)
        return False

def upload_file(filepath, project_id=None, subject_id=None, session_id=None, scan_id=None, assessor_id=None, resource=None, remove=False, removeall=False, fname=None):
    """ Upload the file to a resource information (project/subject/...) from XNAT
        Inputs: 
            filepath: path of the file on your local computer
            project_id: project ID on XNAT
            subject_id: subject ID or label on XNAT
            session_id: session ID or label on XNAT
            scan_id: scan ID on XNAT
            assessor_id: assessor ID or label on XNAT
            resource: resource name on XNAT
            remove: remove files that already exists on the resource.
            removeall: remove all previous files on the resource.
            fname: give a different name for the file on XNAT (e.g: target.nii.gz --> results/target.nii.gz)
                   this will create a folder results and put the file in it for the resource.
        Return:
            status of the upload (True or False)
    """
    status = False
    if not resource:
        print "ERROR: upload_file in XnatUtils: resource argument not provided."
    else:
        xnat = get_interface()
        resource_obj = select_obj(xnat, project_id, subject_id, session_id, scan_id, assessor_id, resource)
        status = upload_file_from_obj(filepath, resource_obj, remove, removeall)
        xnat.disconnect()
    return status

def upload_files_from_obj(filepaths, resource_obj, remove=False, removeall=False):
    """ Upload a list of files to the resource_obj given to the function
        Inputs: 
            filepaths: list of files on your local computer
            resource_obj: resource object on XNAT (select resource for project or subject or session or scan or assessor)
            remove: remove files that already exists on the resource.
            removeall: remove all previous files on the resource.
        Return:
            list of status of the upload (one status per files you want to upload)
    """
    if removeall and resource_obj.exists: #Remove previous resource to upload the new one
        resource_obj.delete()
    status = list()
    for filepath in filepaths:
        status.append(upload_file_from_obj(filepath, resource_obj, remove=remove, removeall=False))
    return status

def upload_files(filepaths, project_id=None, subject_id=None, session_id=None, scan_id=None, assessor_id=None, resource=None, remove=False, removeall=False):
    """ Upload a list of files to a resource information (project/subject/...) from XNAT 
        Inputs: 
            filepaths: list of files on your local computer
            project_id: project ID on XNAT
            subject_id: subject ID or label on XNAT
            session_id: session ID or label on XNAT
            scan_id: scan ID on XNAT
            assessor_id: assessor ID or label on XNAT
            resource: resource name on XNAT
            remove: remove files that already exists on the resource.
            removeall: remove all previous files on the resource.
        Return:
            list of status of the upload (one status per files you want to upload)
    """
    status = [False]*len(filepaths)
    if not resource:
        print "ERROR: upload_files in XnatUtils: resource argument not provided."
    else:
        xnat = get_interface()
        resource_obj = select_obj(xnat, project_id, subject_id, session_id, scan_id, assessor_id, resource)
        status = upload_files_from_obj(filepaths, resource_obj, remove, removeall)
        xnat.disconnect()
    return status

def upload_folder_from_obj(directory, resource_obj, resource_label, remove=False, removeall=False):
    """ Upload folder (all content) to the resource_obj given to the function
        Inputs: 
            directory: folder on your local computer (all content will be upload)
            reosurce_obj: resource object on XNAT (select resource for project or subject or session or scan or assessor)
            remove: remove files that already exists on the resource.
            removeall: remove all previous files on the resource.
        Return:
            status of the upload (True or False)
    """
    if not os.path.exists(directory):
        print """ERROR: upload_folder in XnatUtils: directory {directory} does not exist.""".format(directory=directory)
        return False

    if resource_obj.exists:
        if removeall:
            resource_obj.delete()
        if not remove: #check if any files already exists on XNAT, if yes return FALSE
            for fpath in get_files_in_folder(directory):
                if resource_obj.file(fpath).exists():
                    print """ERROR: upload_folder in XnatUtils: file {file} already found on XNAT. No upload. Use remove/removeall.""".format(file=fpath)
                    return False

    filenameZip = resource_label+'.zip'
    initdir = os.getcwd()
    #Zip all the files in the directory
    os.chdir(directory)
    os.system('zip -r '+filenameZip+' *')
    #upload
    resource_obj.put_zip(os.path.join(directory, filenameZip), extract=True)
    #return to the initial directory:
    os.chdir(initdir)
    return True

def upload_folder(directory, project_id=None, subject_id=None, session_id=None, scan_id=None, assessor_id=None, resource=None, remove=False, removeall=False):
    """ Upload folder (all content) to a resource information (project/subject/...) from XNAT 
        Inputs: 
            directory: folder on your local computer (all content will be upload)
            project_id: project ID on XNAT
            subject_id: subject ID or label on XNAT
            session_id: session ID or label on XNAT
            scan_id: scan ID on XNAT
            assessor_id: assessor ID or label on XNAT
            resource: resource name on XNAT
            remove: remove files that already exists on the resource.
            removeall: remove all previous files on the resource.
        Return:
            status of the upload (True or False)
    """
    status = False
    if not resource:
        print "ERROR: upload_file in XnatUtils: no resource argument provided."
    else:
        xnat = get_interface()
        resource_obj = select_obj(xnat, project_id, subject_id, session_id, scan_id, assessor_id, resource)
        status = upload_folder_from_obj(directory, resource_obj, resource, remove, removeall)
        xnat.disconnect()
    return status

def copy_resource_from_obj(directory, xnat_obj, old_res, new_res):
    """ Copy resource for one object from an old resource to the new resource"""
    #resources objects:
    if xnat_obj.datatype() in ['proc:genProcData', 'fs:fsData']:
        old_resource_obj = xnat_obj.out_resource(old_res)
        new_resource_obj = xnat_obj.out_resource(new_res)
    else:
        old_resource_obj = xnat_obj.resource(old_res)
        new_resource_obj = xnat_obj.resource(new_res)
    #Copy
    fpaths = download_files_from_obj(directory, old_resource_obj)
    if not fpaths:
        return False
    status = upload_folder_from_obj(os.path.join(directory, old_resource_obj.label()), new_resource_obj, new_res)
    #clean director
    clean_directory(directory)
    return status

def copy_resource(directory, project_id=None, subject_id=None, session_id=None, scan_id=None, assessor_id=None, old_res=None, new_res=None):
    """ Copy resource for one xnat object (project/subject/session/scan/assessor) from an old resource to the new resource"""
    status = False
    if not old_res or not new_res:
        print "ERROR: copy_resource in XnatUtils: resource argument (old_res or new_res) not provided."
    else:
        xnat = get_interface()
        xnat_obj = select_obj(xnat, project_id, subject_id, session_id, scan_id, assessor_id)
        status = copy_resource_from_obj(directory, xnat_obj, old_res, new_res)
        xnat.disconnect()
    return status

####################################################################################
#                                4) Other Methods                                  #
####################################################################################
def clean_directory(directory):
    """ Empty a directory"""
    for fname in os.listdir(directory):
        fpath = os.path.join(directory, fname)
        if os.path.isdir(fpath):
            shutil.rmtree(fpath)
        else:
            os.remove(fpath)

def makedir(directory, prefix='TempDir'):
    """ make tmp directory if already exist"""
    if not os.path.exists(directory):
        os.mkdir(directory)
    else:
        today = datetime.now()
        directory = os.path.join(directory, prefix+'_'+str(today.year)+'_'+str(today.month)+'_'+str(today.day))
        if not os.path.exists(directory):
            os.mkdir(directory)
        else:
            clean_directory(directory)
    return directory

def print_args(options):
    """ print arguments for Spider"""
    print "--Arguments given to the spider--"
    for info, value in vars(options).items():
        if value:
            print """{info}: {value}""".format(info=info, value=value)
        else:
            print info, ": Not set. The process might fail without this argument."
    print "---------------------------------"

def get_files_in_folder(folder, label=''):
    """ Get all the files recursively starting from the folder"""
    f_list = list()
    for fpath in os.listdir(folder):
        ffpath = os.path.join(folder, fpath)
        if os.path.isfile(ffpath):
            fpath = check_image_format(fpath)
            if label:
                filename = os.path.join(label, fpath)
            else:
                filename = fpath
            f_list.append(filename)
        else:
            label = os.path.join(label, fpath)
            f_list.extend(get_files_in_folder(ffpath, label))
    return f_list

def check_image_format(fpath):
    """ Check if the path is a nifti or rec image that need to be compress"""
    if fpath.endswith('.nii') or fpath.endswith('.rec'):
        os.system('gzip '+fpath)
        fpath = fpath+'.gz'
    return fpath

def upload_list_records_redcap(rc, data):
    """ Upload data of a dict to a rc project 
        Inputs:
            rc: project on REDCap open with request
            data: list of dictionaries that need to be upload
    """
    upload_data = True
    if isinstance(data, dict):
        data = [data]
    elif isinstance(data, list):
        pass
    else:
        upload_data = False
    if upload_data:
        try:
            response = rc.import_records(data)
            assert 'count' in response
        except AssertionError as e:
            print '      -ERROR: Creation of record failed. The error is the following: '
            print '      ', e
            print response
        except:
            print '      -ERROR: connection to REDCap interupted.'

class CachedImageSession():
    def __init__(self,xnat,proj,subj,sess):
        #self.sess_element = ET.fromstring(xnat.session_xml(proj,sess))
        xml_str = xnat.select('/project/'+proj+'/subject/'+subj+'/experiment/'+sess).get()
        self.sess_element = ET.fromstring(xml_str)
        self.project = proj
        self.subject = subj
        
    def label(self):
        return self.sess_element.get('label')
    
    def get(self,name):        
        value = self.sess_element.get(name)
        if value != None:
            return value
        
        element = self.sess_element.find(name, NS)
        if element != None:
            return element.text
        
        split_array = name.rsplit('/',1)    
        if len(split_array) == 2:
            tag,attr = split_array
            element = self.sess_element.find(tag, NS)
            if element != None:
                value = element.get(attr)
                if value != None:
                    return value
        
        return ''

    def scans(self):
        scan_list = []
        scan_elements = self.sess_element.find('xnat:scans', NS)
        for scan in scan_elements:
            scan_list.append(CachedImageScan(scan,self))
            
        return scan_list

    def assessors(self):
        assr_list = []
        
        assr_elements = self.sess_element.find('xnat:assessors', NS)
        for assr in assr_elements:
            assr_list.append(CachedImageAssessor(assr,self))
        
        return assr_list
   
    def info(self):
        sess_info = {}
    
        sess_info['ID'] = self.get('ID')
        sess_info['label'] = self.get('label')
        sess_info['note'] = self.get('xnat:note')
        sess_info['session_type'] = self.get('session_type')
        sess_info['project_id'] = self.get('project')
        sess_info['original'] = self.get('original')
        sess_info['modality'] = self.get('modality')
        sess_info['UID'] = self.get('UID')
        sess_info['subject_id'] = self.get('xnat:subject_ID')
        sess_info['subject_label'] = self.subject
        sess_info['project_label'] = sess_info['project_id'] 
        sess_info['project'] = sess_info['project_id'] 
        sess_info['subject_ID'] = self.get('xnat:subject_ID')
        sess_info['URI'] = '/data/experiments/'+sess_info['ID'] 
        sess_info['session_label'] = sess_info['label']  
        sess_info['last_updated'] = sess_info['original']
        sess_info['type'] = sess_info['modality']

        return sess_info
        
class CachedImageScan():
    def __init__(self,scan_element,parent):
        self.scan_parent = parent
        self.scan_element = scan_element
        
    def parent(self):
        return self.scan_parent
        
    def label(self):
        return self.scan_element.get('ID')
    
    def get(self,name):
        value = self.scan_element.get(name)
        if value != None:
            return value
        
        element = self.scan_element.find(name, NS)
        if element != None:
            return element.text
        
        tag, attr = name.rsplit(':',1)        
        element = self.scan_element.find(tag, NS)
        if element != None:
            value = element.get(attr)
            if value != None:
                return value
        
        return ''
    
    def info(self):
        scan_info = {}
    
        scan_info['ID'] = self.get('ID')
        scan_info['label'] = self.get('ID')
        scan_info['quality'] = self.get('xnat:quality')
        scan_info['frames']  = self.get('xnat:frames')
        scan_info['note'] = self.get('xnat:note')
        scan_info['type'] = self.get('type')
        scan_info['series_description'] = self.get('xnat:series_description')
        scan_info['project_id'] = self.parent().get('project')
        scan_info['subject_id'] = self.parent().get('xnat:subject_ID')
        scan_info['subject_label'] = self.parent().subject

        scan_info['scan_id'] = scan_info['ID']
        scan_info['scan_label'] = scan_info['label'] 
        scan_info['scan_quality'] = scan_info['quality'] 
        scan_info['scan_note'] = scan_info['note'] 
        scan_info['scan_type'] = scan_info['type']
        scan_info['scan_frames'] = scan_info['frames'] 
        scan_info['scan_description'] = scan_info['series_description']
        
        scan_info['session_id'] = self.parent().get('ID')
        scan_info['session_label'] = self.parent().get('label')
        scan_info['project_label'] = scan_info['project_id'] 
                
        return scan_info

    def resources(self):
        res_list = []
        
        file_elements = self.scan_element.findall('xnat:file', NS)
        for f in file_elements:
            xsi_type = f.get('{http://www.w3.org/2001/XMLSchema-instance}type')
            if xsi_type == 'xnat:resourceCatalog':
                res_list.append(CachedResource(f,self))
            
        return res_list

class CachedImageAssessor():
    def __init__(self,assr_element,parent):
        self.assr_parent = parent
        self.assr_element = assr_element
        
    def parent(self):
        return self.assr_parent
        
    def label(self):
        return self.assr_element.get('label')
    
    def get(self,name):
        value = self.assr_element.get(name)
        if value != None:
            return value
        
        element = self.assr_element.find(name, NS)
        if element != None:
            return element.text
        
        #tag, attr = name.rsplit('/',1)        
        #element = self.assr_element.find(tag, NS)
        #if element != None:
        #    value = element.get(attr)
        #    if value != None:
        #        return value
            
        split_array = name.rsplit('/',1)    
        if len(split_array) == 2:
            tag,attr = split_array
            element = self.assr_element.find(tag, NS)
            if element != None:
                value = element.get(attr)
                if value != None:
                    return value
        
        return ''
        
    def info(self):
        assr_info = {}
        
        assr_info['ID'] = self.get('ID')
        assr_info['label'] = self.get('label')
        assr_info['assessor_id'] = assr_info['ID']
        assr_info['assessor_label'] = assr_info['label']
        assr_info['project_id'] = self.get('project')
        assr_info['project_label'] = assr_info['project_id'] 
        assr_info['subject_id'] = self.parent().get('xnat:subject_ID')
        assr_info['subject_label'] = self.parent().subject
        assr_info['session_id'] = self.parent().get('ID')
        assr_info['session_label'] = self.parent().get('label')
        assr_info['xsiType'] = self.get('{http://www.w3.org/2001/XMLSchema-instance}type').lower()
        
        if (assr_info['xsiType'] == 'fs:fsdata'):
            # FreeSurfer
            assr_info['procstatus'] = self.get('fs:procstatus')
            assr_info['qcstatus'] = self.get('xnat:validation/status')
            assr_info['version'] = self.get('fs:procversion')
            assr_info['jobid'] = self.get('fs:jobid')
            assr_info['jobstartdate'] = self.get('fs:jobstartdate')
            assr_info['memused'] = self.get('fs:memused')
            assr_info['walltimeused'] = self.get('fs:walltimeused')
            assr_info['jobnode'] = self.get('fs:jobnode')
            assr_info['proctype'] = 'FreeSurfer'
            
        elif (assr_info['xsiType'] == 'proc:genprocdata'):
            # genProcData
            assr_info['procstatus'] = self.get('proc:procstatus')
            assr_info['proctype'] = self.get('proc:proctype')
            assr_info['qcstatus'] = self.get('xnat:validation/status')
            assr_info['version'] = self.get('proc:procversion')
            assr_info['jobid'] = self.get('proc:jobid')
            assr_info['jobstartdate'] = self.get('proc:jobstartdate')
            assr_info['memused'] = self.get('proc:memused')
            assr_info['walltimeused'] = self.get('proc:walltimeused')
            assr_info['jobnode'] = self.get('proc:jobnode')
        else:
            print('WARN:unknown xsiType for assessor:'+assr_info['xsiType'])
            
        return assr_info
                    
    def in_resources(self):
        res_list = []
        
        file_elements = self.assr_element.findall('xnat:in/xnat:file', NS)
        for f in file_elements:
            res_list.append(CachedResource(f,self))
            
        return res_list
        
    def out_resources(self):
        res_list = []
            
        file_elements = self.assr_element.findall('xnat:out/xnat:file', NS)
        for f in file_elements:
            res_list.append(CachedResource(f,self))
            
        return res_list
        
class CachedResource():
    def __init__(self,element,parent):
        self.res_parent = parent
        self.res_element = element
   
    def parent(self):
        return self.res_parent
    
    def label(self):
        return self.res_element.get('label')
    
    def get(self,name):
        value = self.res_element.get(name)
        if value != None:
            return value
        
        element = self.res_element.find(name, NS)
        if element != None:
            return element.text
        
        split_array = name.rsplit('/',1)    
        if len(split_array) == 2:
            tag,attr = split_array
            element = self.res_element.find(tag, NS)
            if element != None:
                value = element.get(attr)
                if value != None:
                    return value
            
        return ''

    def info(self):
        res_info = {}

        res_info['URI'] = self.get('URI')
        res_info['label'] = self.get('label')
        res_info['file_size'] = self.get('file_size')
        res_info['file_count'] = self.get('file_count')
        res_info['format'] = self.get('format')
        res_info['content'] = self.get('content')

        return res_info
