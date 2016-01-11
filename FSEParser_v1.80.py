#!/usr/bin/python

# FSEvents Parser Python Script
# ------------------------------------------------------
# Parse FSEvent records from allocated fsevent files and carved gzip files.
# Outputs parsed information to a tab delimited txt file and SQLite database.
# Errors and exceptions are recorded in the exceptions logfile.

# Copyright 2016 G-C Partners, LLC
# Nicole Ibrahim
#
# G-C Partners licenses this file to you under the Apache License, Version
# 2.0 (the "License"); you may not use this file except in compliance with the
# License.  You may obtain a copy of the License at:
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.  See the License for the specific language governing
# permissions and limitations under the License.

import sys
import os
import logging
import struct
import binascii
import gzip
import re
import fileinput
import datetime
import time
import sqlite3

VERSION = '1.80'

EVENTMASK = {
    0x00000000: 'None;',
    0x00000001: 'MustScanSubDirs;',
    0x00000002: 'UserDropped;',
    0x00000004: 'KernelDropped;',
    0x00000008: 'EventIdsWrapped;',
    0x00000010: 'HistoryDone;',
    0x00000020: 'RootChanged;',
    0x00000040: 'Mount;',
    0x00000080: 'Unmount;',
    0x00001000: 'UseCFTypes;',
    0x00002000: 'NoDefer;',
    0x00004000: 'WatchRoot;',
    0x00008000: 'IgnoreSelf;',
    0x00010000: 'ItemIsFile;',
    0x00020000: 'ItemIsDir;',
    0x00040000: 'ItemIsSymlink;',
    0x01000000: 'ItemCreated;',
    0x02000000: 'ItemRemoved;',
    0x04000000: 'ItemInodeMetaMod;',
    0x08000000: 'ItemRenamed;',
    0x10000000: 'ItemModified;',
    0x20000000: 'ItemFinderInfoMod;',
    0x40000000: 'ItemChangeOwner;',
    0x80000000: 'ItemXattrMod;'
}

db_filename = 'fsevents.sqlite'

def Main():
    msg = "FSEvents Parser Python Script, Version %s\
            \n\nParse FSEvent records from allocated and carved files.\
            \nOutputs parsed information to a tab delimited txt file and SQLite database.\
            \nErrors and exceptions are recorded in the exceptions logfile.\
            \n--------------------------------------\
            \nCommand Syntax: > python 'FSEParser.py' 'e:\case\exports\path_to_fsevents'\n" % (VERSION)
    try:
        fsevents_dir = sys.argv[1]
    except:
        # Print welcome message if no fsevents dir given
        print msg
        # Exit the script
        sys.exit(0)
        
    # Print welcome message
    print msg
    
    # Create SQLite database and setup transaction
    createSqliteDB()
    
    # Process fsevents
    FSEventHandler(fsevents_dir)

    # Commit transaction
    sqlCon.commit()
    
    # Cleanup
    sqlCon.close()
    
def EnumerateFlags(flag,flag_mapping):
    # Reset string based flags to null
    str_flag = ''
    
    # Iterate through flags 
    for i in flag_mapping:
        if (i & flag):
            str_flag = ''.join([str_flag,flag_mapping[i]])
    return str_flag

class FSEventHandler():
    def __init__(self,path):
        self.path = path
        self.files = []
        self.pages = []
        self.filename = ''
        # Initialize statistic counters
        self.all_records_count = 0
        self.all_files_count = 0
        self.parsed_file_count = 0
        self.error_file_count = 0
        # Try to open the output files
        try:
            ## Output file for parsed records
            self.outfile = open('FSEvents-Parsed_Records-tab_delimited.txt','w')      
            ## Output log file for exceptions
            self.logfile = open('FSEvents-EXCEPTIONS_LOG.txt','w')       
        except:
            # Print error to command prompt if unable to open files
            print "\n---------------ERROR----------------\
            \nOne of the following output files are currently in use by another program.\
            \n -FSEvents-Parsed_Records.tsv\n -FSEvents-Exception_Log.txt\
            \nPlease ensure that these files are closed. Then rerun the parser."
            sys.exit(0)

        # Continue to next phase of fsevent parsing
        self._GetFsEventFiles()

        
    def _GetFsEventFiles(self):
        '''
        This section will iterate through each file in the fsevents dir provided,
        It will first open each file, search for an sld header in the file (uncompressed)
        If no sld header was found, it will then try to unzip the gzip. If it is unable
        to un-compress the gzip file, it will write an entry in the logfile. If gzip
        is successful, the script will check for a sld header in the uncompressed gzip.
        If found, the contents of the gzip will be placed into a buffer and passed to the
        next phase of processing.
        '''
        # Print the header columns to the output file #
        Output.PrintColumns(self.outfile)                       
        
        #iterate through each file in supplied fsevents dir
        for filename in os.listdir(self.path):
            self.all_files_count+=1
            print "Trying\t%s" % (filename)
            # Create fullpath to source fsevent file
            f_name = os.path.join(self.path,filename)
            self.filename = filename

            try:
                # try to unzip file then try to read
                self.files = gzip.GzipFile(f_name,'rb')
                # Place unzipped contents into a buffer
                buf = self.files.read()
            except:
                # If unable to unzip or read file, respond with err
                self.logfile.write("%s\tError: unable to un-compress gzip file\n" % (f_name))
                self.error_file_count+=1
                # Continue to the next file in the fsevents directory
                continue
            # If try is success, check for sld headers in the current file
            chk = FSEventHandler.SLDHeaderSearch(self,buf,f_name)
            # If check for sld returns false, write information to logfile
            if chk is False:
                self.logfile.write("%s\tError: Unable to find a SLD1 header.\n" % (f_name))
                # Continue to the next file in the fsevents directory
                self.error_file_count+=1
                continue
            # If checks pass, pass the buffer to be parsed
            self.parsed_file_count+=1
            FSEventHandler.Parse(self,buf)
        # Close output files
        self.outfile.close()
        self.logfile.close()
        # Print stats
        print "\n---------------------\n"
        print "FINISHED PARSING: See exceptions log for parsing errors."
        print "All Files Attempted: %d\nAll Parsed Files: %d\nFiles with Errors: %d\nTotal Records Parsed: %d" % (self.all_files_count,self.parsed_file_count,self.error_file_count,self.all_records_count)

    def SLDHeaderSearch(self,buf,f_name):
        '''
        This will take the entire uncompressed fsevents file and
        search for every occurance of the SLD1 page header.
        There can be more than one SLD header in an fsevents file.
        The start and end offsets are stored and used for parsing
        the records contained within each SLD page.
        '''
        raw_file = buf
        self.file_size = len(buf)
        
        sld_count = 0
        self.my_slds = []
        
        # for each search hit that contains '1SLD'
        for match in re.finditer('\x31\x53\x4c\x44', raw_file):
            '''
            For each search hit, store offsets in a dict
            Account for exceptions using following if statements
            '''
            if sld_count==0:
                # Since this is the first record found
                # Assigned the file size as the end offset of sld [0]
                start_offset = match.regs[0][0]
                end_offset = self.file_size 
            if sld_count==1:
                # Since this is second sld found assign end
                # offset to previously found sld
                start_offset = match.regs[0][0]
                self.my_slds[sld_count-1]['End Offset'] = start_offset
            if sld_count>1:
                # For SLDs found after the first two
                # Set the end_off to the curr file size, set start to prev sld end
                end_offset = self.file_size
                self.my_slds[sld_count-1]['End Offset'] = match.regs[0][0]
                start_offset = self.my_slds[sld_count-1]['End Offset']
                
            # Use a temp dict to assignment start and end off of current sld location            
            temp_dict = [{'Start Offset': start_offset, 'End Offset': end_offset}]
            
            # Append current sld information to the SLD dictionary
            self.my_slds.append(temp_dict[0])
            del temp_dict
            sld_count+=1

        # If the search does not find a valid sld header, write exception to logfile
        if sld_count==0:
            # Return false to caller so that the next file will be searched
            return False
        # Return true so that the SLDs found can be parsed
        return True
    
    def FindDate(self,raw_file):
        '''
        Search within current file and find ASL log files that contain date.
        The date(s) found in the current file will be assigned to all records parsed.
        private/var/log/asl/YYYY.MM.DD.????.asl
        '''
        # Reset time_range value to null
        self.time_range = ''
        prev_temp = ''
        
        # Start searching within fsevent file for asl file matches
        # This search matches not only the asl full path, it also
        # Checks the record flags for that file. If the asl file has
        # a created flag set or modified flag, then it will match
        for match in re.finditer('\x70\x72\x69\x76\x61\x74\x65\x2f\x76\x61\x72\x2f\x6c\x6f\x67\x2f\x61\x73\x6c\x2f[\x30-\x39]{4}\x2e[\x30-\x39]{2}\x2e[\x30-\x39]{2}\x2e[\x30-\x7a]{2,5}\x2e\x61\x73\x6c[\x00-\xFF]{9}[\x10\|\x01|\x11|\x21|\x41|\x81]', raw_file):
            # Assign our variables for the current match
            t_temp = ''
            
            # t_start uses the start offset of the match
            # The date is located 20 chars into the match
            t_start = match.regs[0][0]+20
            
            # The date is 10 chars long in the format of yyyy.mm.dd
            # So the end offset for the date is 10 chars after start
            t_end = t_start+10
            
            # Strip the date from the fsevent file
            t_temp = raw_file[t_start:t_end]
            
            # Account for multiple dates found in one fsevent file
            if len(self.time_range)==0:
                # If this is the first match, assign it
                self.time_range = t_temp
                prev_temp = t_temp
            elif prev_temp == t_temp:
                # If other dates are found, but are the same as prev do nothing
                continue
            else:
                # Otherwise, appear the new date found to all previous ones
                self.time_range = self.time_range+","+t_temp
                prev_temp = t_temp
        
        # If no matches were found, set the time to unknown
        if len(self.time_range)==0:
            self.time_range = "UNKNOWN"
    
    def Parse(self,buf):
        ''' Initialize variables '''
        pg_count = 0
        self.record_count = 0
        # Call the date finder for current fsevent file
        FSEventHandler.FindDate(self,buf)
        
        for i in self.my_slds:
            print "SLD Header found. Parsing records"
            # Assign current SLD offsets
            start_offset = self.my_slds[pg_count]['Start Offset']
            end_offset = self.my_slds[pg_count]['End Offset']
            
            # Extract the raw SLD page from the fsevents file
            raw_page = buf[start_offset:end_offset]
            self.page_offset = start_offset
            
            # Pass the raw page a start offset to find records within page
            FSEventHandler.FindPageRecords(
                self,
                raw_page,
                start_offset
                )
            # Increment the sld page count by 1
            pg_count+=1
            
    def FindPageRecords(self,page_buf,page_start_off):
        '''
        Input values are starting offset of current page and
        end offset of current page within the current fsevent file
        This definition will identify all records within a given page.
        '''
        ## Initialize variables ##
        filename = ''
        char = ''
        
        # Start offset of first record to be parsed within current sld page
        start_offset = 12
        end_offset = 13
        len_buf = len(page_buf)
        
        # Call the file header parser for current sld
        try:
            file_header = FsEventFileHeader(
                page_buf[:13],
                self.filename
            )
        except:
            self.logfile.write(
                "%s\tError: Unable to parse file header at offset %d" % (
                    self.filename,
                    self.page_offset
                    )
                )
        '''
        Within this while loop, we will iterate through each char
        until we find 00. That is our end of filename marker for
        the current record. We will then grab the 12 bytes after
        the filename, that will be our raw record to parse.
        The while ensures that we parse all records within the current
        page. The start offset is incremented after each record is parsed.
        '''
        while len_buf > start_offset :
            # Grab the first char
            char = page_buf[start_offset:end_offset].encode('hex')
            '''
            The following if statement will test to see if the current
            char equals 00. 00 Indicated the end of the fullpath for
            the current record.
            '''
            if char!='00':
                # Append the current char to the full path for current record
                filename = filename + char
                # Increment the offsets by one
                start_offset+=1
                end_offset+=1
                # Continue the while loop
                continue
            elif char=='00':
                # When 00 is found, then this is the end of fullpath
                # Increment the offsets by 13, this will be the start of next full path
                start_offset+=13
                end_offset+=13
                pass
            '''
            After 00 is found, we start parsing the record full path
            and the raw record which contains the wd and reason flags.
            '''
            # Decode filename that was stored as hex 
            filename = filename.decode('hex')
            # Store the record length, fullpath length and len of raw record (13)
            record_len = len(filename)+13
            
            # Account for records that do not have a fullpath
            if record_len==13:
                # Assign a null value to the path
                filename = "NULL VALUE"
            # Increment the current record count by 1
            self.record_count+=1
            self.all_records_count+=1
            
            # Assign raw record offsets #
            r_start = start_offset-12
            r_end = start_offset

            # Strip raw record from page buffer #
            raw_record = page_buf[r_start:r_end]

            # Strip mask from buffer and encode as hex #
            mask_hex = "0x"+raw_record[8:].encode('hex')

            record_off = start_offset+page_start_off

            # Pass the raw record for parsing
            try:
                record = FSEventRecord(raw_record,record_off)
                pass
            except:
                self.logfile.write(
                    "%s\tError: Unable to parse event record at offset %d" % (self.filename,self.page_offset)
                    )
                self.record_count-=1
                continue
            
            # Assign our current records attributes
            attributes = {
                'record_filename':filename,
                'record_mask':record.mask,
                'record_mask_hex':mask_hex,
                'asl_name_date_stripped':self.time_range,
                'record_wd':record.wd,
                'record_number_parse_order':self.record_count,
                'record_length':record_len,
                'max_wd_record_number':file_header.fsoffset,
                'record_end_relative_offset':record.file_offset,
                'current_page_size':file_header.filesize,
                'source_fsevents_filesize':self.file_size,
                'source_fsevents_file':self.filename,
                'source_fsevents_path':self.path,
                'file_header-unknown_hex':file_header.unknown_hex,
                'file_header-unknown_int':file_header.unknown_int
            }
            # Valid records will have a non-zero wd val
            if record.wd!=0:
                # Print parsed record attributes
                output = Output(attributes)
                
                # Print the parsed record to output file
                output.Print(self.outfile)
                filename = ''
            # Else, if wd is 0 write exception to logfile
            else:
                self.logfile.write(
                    "%s\tError: Unable to parse record entry for record filename '%s' at offset %d" % (
                        self.filename,
                        file_header.filename,
                        record.file_offset
                        )
                    )
                self.record_count-=1
            # Reset filename to null
            filename = ''

class FsEventFileHeader():
    def __init__(self,buf,filename):
        # Name and path of current source fsevent file
        self.filename = filename
        # Page header '1SLD'
        self.signature = buf[0:4]
        # Unknown raw values in SLD1 header
        self.unknown_raw = buf[4:8]
        # Unknown hex version 
        self.unknown_hex = "0x"+buf[4:8].encode("hex")
        # Unknown integer version
        self.unknown_int = struct.unpack("<I", self.unknown_raw)[0]
        # Size of current SLD page
        self.filesize = struct.unpack("<I", buf[8:12])[0]
        
        # Account for carved gzip files with variable name
        regexp = re.compile(r'^.*[\][0-9a-fA-F]{16}$')
        
        # Use the name of allocated fsevents files
        # This is related to the max event ID before
        # the OS creates the next fsevent files.
        if regexp.search(filename) is not None:
            self.fsoffset_hex = os.path.basename(self.filename)
            self.fsoffset = int(self.fsoffset_hex,16)
        else:
            # Carved gzip files that do not conform to
            # standard fsevent filename will be marked unknown/0
            self.fsoffset_hex = "UNKNOWN"
            self.fsoffset = 0
                   
class FSEventRecord(dict):
    def __init__(self,buf,offset):
        # Offset of the record within the fsevent file
        self.file_offset = offset
        # Raw record hex version
        self.header_hex = binascii.b2a_hex(buf)
        # Record wd or event id
        self.wd = struct.unpack("<Q", buf[0:8])[0]
        # Record wd hex version
        self.wd_hex = hex(self.wd)
        # Enumerate mask flags, string version
        self.mask = EnumerateFlags(
            struct.unpack(">I", buf[8:12])[0],
            EVENTMASK
        )
            
class Output(dict):
    COLUMNS = [
        'record_filename',
        'record_mask',
        'record_mask_hex',
        'asl_name_date_stripped',
        'record_wd',
        'record_number_parse_order',
        'record_length',
        'max_wd_record_number',
        'record_end_relative_offset',
        'current_page_size',
        'source_fsevents_filesize',
        'source_fsevents_file',
        'source_fsevents_path',
        'file_header-unknown_hex',
        'file_header-unknown_int'
    ]
    @staticmethod
    def PrintColumns(outfile):
        values = []
        for key in Output.COLUMNS:
            values.append(str(key))
        row = "\t".join(values)
        row = row + "\n"
        
        outfile.write(row)
        
    def __init__(self,attribs):
        self.update(attribs)
        
    def Print(self,outfile):
        values = []
        for key in Output.COLUMNS:
            values.append(str(self[key]))
        
        out = "\t".join(values)
        out = out + "\n"
        outfile.write(out)       
        
        valsToInsert = "\",\"".join(values)
        valsToInsert = "\"" + valsToInsert + "\""
        
        insertSqliteDB(valsToInsert)

class VolumeHandler():
    def __init__(self,volume):
        vfh = open(volume,'rb')
        vfh.seek(1024 + 16)
        ts = struct.unpack("<I",vfh.read(4))
        
        self.createtime = GetTimeFromHfsp(ts)
        
def GetTimeFromHfsp(timestamp):
    new_dt = HFSP_EPOCH + datetime.timedelta(seconds=timestamp)
    return new_dt

def createSqliteDB():
    tableSchema = "CREATE TABLE [fsevents](\
                  [record_filename] [TEXT] NULL,\
                  [record_mask] [TEXT] NULL,\
                  [record_mask_hex] [TEXT] NULL,\
                  [asl_name_date_stripped] [TEXT] NULL,\
                  [record_wd] [TEXT] NULL,\
                  [record_number_parse_order] [TEXT] NULL,\
                  [record_length] [TEXT] NULL,\
                  [max_wd_record_number] [TEXT] NULL,\
                  [record_end_relative_offset] [TEXT] NULL,\
                  [current_page_size] [TEXT] NULL,\
                  [source_fsevents_filesize] [TEXT]NULL,\
                  [source_fsevents_file] [TEXT] NULL, \
                  [source_fsevents_path] [TEXT] NULL, \
                  [file_header-unknown_hex] [TEXT] NULL,\
                  [file_header-unknown_int] [TEXT] NULL)"
    
    #if database already exists delete it
    try:
        if(os.path.isfile(db_filename)):
            os.remove(db_filename)
    except WindowsError:
        print "\nFSEvents Parser Python Script, Version %s\n\
        \n-----------ERROR------------\
        \nThe following output file is currently in use by another program.\
        \n -fsevents.sqlite\
        \n\nPlease ensure that the file is closed. Then rerun the parser." % (VERSION)
        sys.exit(0)
        
    #create database file if it doesn't exist
    db_is_new = not os.path.exists(db_filename)
    
    #setup global
    global sqlCon

    #with sqlite3.connect(db_filename) as conn:
    sqlCon = sqlite3.connect(db_filename)
    
    if db_is_new:
        #Create table if it's a new database
        sqlCon.execute(tableSchema)

    #setup global
    global sqlTran
        
    #setup transaction cursor and return it
    sqlTran = sqlCon.cursor()
    
            
def insertSqliteDB(valsToInsert):
    #with sqlite3.connect(db_filename) as conn:
    insertStatement = "\
        insert into fsevents (\
        [record_filename],\
        [record_mask],\
        [record_mask_hex],\
        [asl_name_date_stripped],\
        [record_wd],\
        [record_number_parse_order],\
        [record_length],\
        [max_wd_record_number],\
        [record_end_relative_offset],\
        [current_page_size],\
        [source_fsevents_filesize],\
        [source_fsevents_file],\
        [source_fsevents_path],\
        [file_header-unknown_hex],\
        [file_header-unknown_int]\
        ) values (" + valsToInsert + ")"
    try:
        sqlTran.execute(insertStatement)
    except sqlCon.Error:
        print("insert failed!")

if __name__ == '__main__':
    Main()
    
