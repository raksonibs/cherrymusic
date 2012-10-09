#!/usr/bin/python3
#
# CherryMusic - a standalone music server
# Copyright (c) 2012 Tom Wallroth & Tilman Boerner
#
# Project page:
#   http://fomori.org/cherrymusic/
# Sources on github:
#   http://github.com/devsnd/cherrymusic/
#
# CherryMusic is based on
#   jPlayer (GPL/MIT license) http://www.jplayer.org/
#   CherryPy (BSD license) http://www.cherrypy.org/
#
# licensed under GNU GPL version 3 (or later)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>
#

import os
import re
import sqlite3

from collections import deque
from operator import itemgetter
from time import time

import cherrymusicserver as cherry
from cherrymusicserver import log
from cherrymusicserver import util
from cherrymusicserver.progress import ProgressTree, ProgressReporter

scanreportinterval = 1
AUTOSAVEINTERVAL = 100
debug = False
performanceTest = False
keepInRam = False

if debug:
    log.level(log.DEBUG)

class SQLiteCache(object):
    def __init__(self, DBFILENAME):
        self.validate_basedir()
        setupDB = not os.path.isfile(DBFILENAME) or os.path.getsize(DBFILENAME) == 0
        setupDB |= DBFILENAME == ':memory:' #always rescan when using ram db.
        log.i('Starting database... ')

        self.conn = sqlite3.connect(DBFILENAME, check_same_thread=False)
        self.db = self.conn.cursor()
        self.rootDir = cherry.config.media.basedir.str

        if setupDB:
            log.i('Creating tables...')
            self.__create_tables()
            log.i('Creating index for dictionary and search tables... ')
            self.__create_indexes()
            log.i('done.')
            log.i('Connected to Database. (' + DBFILENAME + ')')
        #I don't care about journaling!
        self.conn.execute('PRAGMA synchronous = OFF')
        self.conn.execute('PRAGMA journal_mode = MEMORY')


    def __table_exists(self, name):
        return bool(self.conn.execute('SELECT name FROM sqlite_master'
                                      ' WHERE type="table" AND name=?', (name,)
                                      ).fetchall())


    def __table_is_empty(self, name):
        if not self.__table_exists(name):
            raise ValueError("table does not exist: %s" % name)
        query = 'SELECT rowid FROM %s LIMIT 1' % (name,)
        res = self.conn.execute(query).fetchall()
        return not bool(res)


    def __create_tables(self):
        self.__drop_tables()
        self.conn.execute('CREATE TABLE files ('
                          ' parent int NOT NULL,'
                          ' filename text NOT NULL,'
                          ' filetype text,'
                          ' isdir int NOT NULL)')
        self.conn.execute('CREATE TABLE dictionary (word text NOT NULL)')
        self.conn.execute('CREATE TABLE search ('
                          ' drowid int NOT NULL,'
                          ' frowid int NOT NULL)')


    def __drop_tables(self):
        self.conn.execute('DROP TABLE IF EXISTS files')
        self.conn.execute('DROP TABLE IF EXISTS dictionary')
        self.conn.execute('DROP TABLE IF EXISTS search')


    def __create_indexes(self):
        self.conn.execute('CREATE INDEX IF NOT EXISTS idx_files_parent'
                          ' ON files(parent)')
        self.conn.execute('CREATE INDEX IF NOT EXISTS idx_dictionary'
                          ' ON dictionary(word)')
        self.conn.execute('CREATE INDEX IF NOT EXISTS idx_search'
                          ' ON search(drowid,frowid)')
        self.conn.execute('CREATE INDEX IF NOT EXISTS idx_search_rvs'
                          ' ON search(frowid,drowid)')


    def __drop_indexes(self):
        self.conn.execute('DROP INDEX IF EXISTS idx_files_parent')
        self.conn.execute('DROP INDEX IF EXISTS idx_dictionary')
        self.conn.execute('DROP INDEX IF EXISTS idx_search')
        self.conn.execute('DROP INDEX IF EXISTS idx_search_rvs')


    def isEmpty(self):
        return self.__table_is_empty('files')


    @classmethod
    def searchterms(cls, searchterm):
        words = re.findall('(\w+)', searchterm.replace('_', ' '))
        return list(map(lambda x:x.lower(), words))


    def fetchFileIds(self, terms):
        resultlist = []
        for term in terms:
            query = '''SELECT search.frowid FROM dictionary JOIN search ON search.drowid = dictionary.rowid WHERE dictionary.word = ?'''
            limit = ' LIMIT 0, 250' #TODO add maximum db results as configuration parameter
            log.d('Search term: ' + term)
            sql = query + limit
            if performanceTest:
                log.d('Query used: ' + sql)
            self.db.execute(sql, (term,))
            resultlist += self.db.fetchall()

        return resultlist

    def searchfor(self, value, maxresults=10):
        starttime = time()
        self.db = self.conn.cursor()
        terms = SQLiteCache.searchterms(value)
        if debug:
            log.d('searchterms')
            log.d(terms)
        results = []
        resultfileids = {}

        log.d('querying terms: ' + str(terms))
        perf()
        fileids = self.fetchFileIds(terms)
        perf('file id fetching')

        if debug:
            log.d('fileids')
            log.d(fileids)
        for fileid in fileids:
            if fileid in resultfileids:
                resultfileids[fileid] += 1
            else:
                resultfileids[fileid] = 1

        if debug:
            log.d('all file ids')
            log.d(resultfileids)
        #sort items by occurences and only return maxresults
        sortedresults = sorted(resultfileids.items(), key=itemgetter(1), reverse=True)
        #sortedresults = sortedresults[:min(len(resultfileids),maxresults)]
        if debug:
            log.d('sortedresults')
            log.d(sortedresults)
        bestresults = list(map(itemgetter(0), sortedresults))
        if debug:
            log.d('bestresults')
            log.d(bestresults)
        perf()
        for fileidtuple in bestresults:
            results.append(self.fullpath(fileidtuple[0]))
        perf('querying fullpaths')
        if debug:
            log.d('resulting paths')
            log.d(results)
        if performanceTest:
            log.d('overall search took ' + str(time() - starttime) + 's')
        return results

    def fullpath(self, filerowid):
        path = ''
        parent = None
        while(not parent == -1):
            cursor = self.conn.cursor()
            cursor.execute('''SELECT parent, filename, filetype FROM files WHERE rowid=? LIMIT 0,1''', (filerowid,))
            parent, filename, fileext = cursor.fetchone()
            path = os.path.join(filename + fileext, path)
            filerowid = parent
        return os.path.dirname(path)

    def register_file_with_db(self, fileobj):
        """add data in File object to relevant tables in media database"""
        try:
            self.add_to_file_table(fileobj)
            word_ids = self.add_to_dictionary_table(fileobj.name)
            self.add_to_search_table(fileobj.uid, word_ids)
        except UnicodeEncodeError as e:
            log.e("wrong encoding for filename '%s' (%s)", fileobj.relpath, e.__class__.__name__)


    def add_to_file_table(self, fileobj):
        cursor = self.conn.execute('INSERT INTO files VALUES (?,?,?,?)', (fileobj.parent.uid if fileobj.parent else -1, fileobj.name, fileobj.ext, 1 if fileobj.isdir else 0))
        rowid = cursor.lastrowid
        fileobj.uid = rowid
        return [rowid]


    def add_to_dictionary_table(self, filename):
        word_ids = []
        for word in set(SQLiteCache.searchterms(filename)):
            wordrowid = self.conn.execute('''SELECT rowid FROM dictionary WHERE word = ? LIMIT 0,1''', (word,)).fetchone()
            if wordrowid is None:
                wordrowid = self.conn.execute('''INSERT INTO dictionary VALUES (?)''', (word,)).lastrowid
            else:
                wordrowid = wordrowid[0]
            word_ids.append(wordrowid)
        return word_ids


    def add_to_search_table(self, file_id, word_id_seq):
        self.conn.executemany('INSERT INTO search VALUES (?,?)',
                              ((wid, file_id) for wid in word_id_seq))


    def remove_recursive(self, fileobj, progress=None):
        '''recursively remove fileobj and all its children from the media db.'''


        if progress is None:
            log.i(
                  'removing dead reference(s): %s "%s"',
                  'directory' if fileobj.isdir else 'file',
                  fileobj.relpath,
                  )
            factory = None
            remove = lambda item: self.remove_file(item)
        else:
            def factory(new, pnt):
                if pnt is None:
                    return (new, None, progress)
                return (new, pnt, pnt[2].spawnchild('[-] ' + new.relpath))
            remove = lambda item: (self.remove_file(item[0]), item[2].tick())

        deld = 0
        try:
            with self.conn:
                for item in self.db_recursive_filelister(fileobj, factory):
                    remove(item)
                    deld += 1
        except Exception as e:
            log.e('error while removing dead reference(s): %s', e)
            log.e('rolled back to safe state.')
            return 0
        else:
            return deld


    def remove_file(self, fileobj):
        '''removes a file entry from the db, which means removing: 
            - all search references,
            - all dictionary words which were orphaned by this,
            - the reference in the files table.'''
        try:
            dead_wordids = self.remove_from_search(fileobj.uid)
            self.remove_all_from_dictionary(dead_wordids)
            self.remove_from_files(fileobj.uid)
        except Exception as exception:
            log.ex(exception)
            log.e('error removing entry for %s', fileobj.relpath)
            raise exception


    def remove_from_search(self, fileid):
        '''remove all references to the given fileid from the search table.
        returns a list of all wordids which had their last search references
        deleted during this operation.'''
        foundlist = self.conn.execute(
                            'SELECT drowid FROM search' \
                            ' WHERE frowid=?', (fileid,)) \
                            .fetchall()
        wordset = set([t[0] for t in foundlist])

        self.conn.execute('DELETE FROM search WHERE frowid=?', (fileid,))

        for wid in set(wordset):
            count = self.conn.execute('SELECT count(*) FROM search'
                                      ' WHERE drowid=?', (wid,)) \
                                      .fetchone()[0]
            if count:
                wordset.remove(wid)
        return wordset


    def remove_all_from_dictionary(self, wordids):
        '''deletes all words with the given ids from the dictionary table'''
        if not wordids:
            return
        args = list(zip(wordids))
        self.conn.executemany('DELETE FROM dictionary WHERE rowid=(?)', args)


    def remove_from_files(self, fileid):
        '''deletes the given file id from the files table'''
        self.conn.execute('DELETE FROM files WHERE rowid=?', (fileid,))


    def db_recursive_filelister(self, fileobj, factory=None):
        """generator: enumerates fileobj and children listed in the db as File 
        objects. each item is returned before children are fetched from db.
        this means that fileobj gets bounced back as the first return value."""
        if factory is None:
            queue = deque((fileobj,))
            while queue:
                item = queue.popleft()
                yield item
                queue.extend(self.fetch_child_files(item))
        else:
            queue = deque((factory(fileobj, None),))
            child = lambda parent: lambda item: factory(item, parent)
            while queue:
                item = queue.popleft()
                yield item
                queue.extend(map(child(item), self.fetch_child_files(item[0])))


    def fetch_child_files(self, fileobj, sort=True, reverse=False):
        '''fetches from files table a list of all File objects that have the
        argument fileobj as their parent.'''
        id_tuples = self.conn.execute(
                            'SELECT rowid, filename, filetype, isdir' \
                            ' FROM files where parent=?', (fileobj.uid,)) \
                            .fetchall()
        if sort:
            id_tuples = sorted(id_tuples, key=lambda t: t[1], reverse=reverse)
        return (File(name + ext,
                     parent=fileobj,
                     isdir=False if isdir == 0 else True,
                     uid=uid) for uid, name, ext, isdir in id_tuples)


    def validate_basedir(self):
        basedir = cherry.config.media.basedir.str
        if not basedir:
            raise AssertionError('basedir is not set')
        if not os.path.isabs(basedir):
            raise AssertionError('basedir must be absolute path: %s' % basedir)
        if not os.path.exists(basedir):
            raise AssertionError("basedir doesn't exist: %s" % basedir)
        if not os.path.isdir(basedir):
            raise AssertionError("basedir is not a directory: %s" % basedir)


    @util.timed
    def full_update(self):
        '''verify complete media database against the filesystem and make
        necesary changes.'''

        log.i('running full update...')
        firstupdate = False
        if not self.__table_exists('files'):
            firstupdate = True
            self.__create_tables()
        elif self.__table_is_empty('files'):
            firstupdate = True
        if firstupdate:
            log.d('firstupdate: running without indexes')
            self.__drop_indexes()

        try:
            self.update_db_recursive(cherry.config.media.basedir.str, skipfirst=True)
        except:
            log.e('error during media update. database update incomplete.')
        else:
            log.i('media database update complete.')
        finally:
            if firstupdate:
                log.i('creating indexes')
                self.__create_indexes()
            log.i('update run finished!')


    def update_db_recursive(self, fullpath, skipfirst=False):
        '''recursively update the media database for a path in basedir'''

        from collections import namedtuple
        Item = namedtuple('Item', 'infs indb parent progress')
        def factory(fs, db, parent):
            fileobj = fs if fs is not None else db
            name = fileobj.relpath or fileobj.fullpath if fileobj else None
            if parent is None:
                progress = ProgressTree(name=name)
                maxlen = lambda s: util.trim_to_maxlen(50, s)
                progress.reporter = ProgressReporter(lvl=1, namefmt=maxlen)
            else:
                progress = parent.progress.spawnchild(name)
            return Item(fs, db, parent, progress)

        log.d('recursive update for %s', fullpath)
        generator = self.enumerate_fs_with_db(fullpath, itemfactory=factory)
        skipfirst and generator.send(None)
        adds_without_commit = 0
        add = 0
        deld = 0
        try:
            with self.conn:
                for item in generator:
                    infs, indb, progress = (item.infs, item.indb, item.progress)
                    if infs and indb:
                        if infs.isdir != indb.isdir:
                            progress.name = '[±] ' + progress.name
                            deld += self.remove_recursive(indb, progress)
                            self.register_file_with_db(infs)
                            adds_without_commit = 1
                        else:
                            progress.name = '[=] ' + progress.name
                    elif indb:
                        progress.name = '[-] ' + progress.name
                        deld += self.remove_recursive(indb, progress)
                        adds_without_commit = 0
                        continue    # progress ticked by remove; don't tick again
                    elif infs:
                        self.register_file_with_db(item.infs)
                        adds_without_commit += 1
                        progress.name = '[+] ' + progress.name
                    if adds_without_commit == AUTOSAVEINTERVAL:
                        self.conn.commit()
                        add += adds_without_commit
                        adds_without_commit = 0
                    progress.tick()
        except Exception as exc:
            log.e("error while updating media: %s %s", exc.__class__.__name__, exc)
            log.e("rollback to previous commit.")
            raise exc
        finally:
            add += adds_without_commit
            log.i('items added %d, removed %d', add, deld)


    def enumerate_fs_with_db(self, startpath, itemfactory=None):
        '''
        Starting at `startpath`, enumerates path items containing representations 
        for each path as it exists in the filesystem and the database, 
        respectively.
        
        `startpath` and `basedir` need to be absolute paths, with `startpath`
        being a subtree of `basedir`. However, no checks are being promised to
        enforce the latter requirement.
        
        Iteration is depth-first, but each path is returned before its children
        are determined, to enable recursive corrective action like deleting a
        whole directory from the database at once. Accordingly, the first item
        to be returned will represent `startpath`. This item is guaranteed to be
        returned, even if `startpath` does not exist in filesystem and database;
        all other items will have at least one existing representation.
        
        `basedir`, should it happen to equal `startpath`, will be returned as an
        item. It is up to the caller to properly deal with it.
        
        Each item has the following attributes: `infs`, a File object
        representing the path in the filesystem; `indb`, a File object
        representing the path in the database; and `parent`, the parent item.
        All three can be None, signifying non-existence. 
        
        It is possible to customize item creation by providing an `itemfactory`.
        The argument must be a callable with the following parameter signature:
            itemfactory(infs, indb, parent [, optional arguments])
        and must return an object satisfying the above requirements for an item.
        '''
        from collections import OrderedDict
        basedir = cherry.config.media.basedir.str
        Item = itemfactory
        if Item is None:
            from collections import namedtuple
            Item = namedtuple('Item', 'infs indb parent')
        assert os.path.isabs(startpath), 'argument must be an abolute path: "%s"' % startpath
        assert startpath.startswith(basedir), 'argument must be a path in basedir (%s): "%s"' % (basedir, startpath)

        fsobj = File(startpath) if os.path.exists(startpath) else None
        dbobj = self.db_lookup(startpath)
        stack = deque()
        stack.append(Item(fsobj, dbobj, None))
        while stack:
            item = stack.pop()
            yield item
            dbchildren = {}
            if item.indb:
                dbchildren = OrderedDict((
                                   (f.basename, f)
                                   for f in self.fetch_child_files(item.indb)
                                   ))
            if item.infs and item.infs.isdir:
                for fs_child in File.inputfilter(item.infs.children()):
                    db_child = dbchildren.pop(fs_child.basename, None)
                    stack.append(Item(fs_child, db_child, item))
            for db_child in dbchildren.values():
                stack.append(Item(None, db_child, item))
            del dbchildren


    def db_lookup(self, fullpath):
        '''Finds an absolute path in the file database. If found, returns
        a File object matching the database record; otherwise, returns None.
        Paths matching a media basedir are a special case: these will yield a
        File object with an invalid record id matching the one listed by its 
        children.'''
        assert os.path.isabs(fullpath)
        basedir = cherry.config.media.basedir.str
        if not fullpath.startswith(basedir):
            return None
        relpath = fullpath[len(basedir):].strip(os.path.sep)
        root = File(basedir, isdir=True, uid= -1)
        if not relpath:
            return root
        file = root
        for part in relpath.split(os.path.sep):
            found = False
            for child in self.fetch_child_files(file):  # gotta be ugly: don't know if name/ext split in db
                if part == child.basename:
                    found = True
                    file = child
                    break
            if not found:
                return None
        return file


def perf(text=None):
    global __perftime
    if text == None:
        __perftime = time()
    else:
        log.d(text + ' took ' + str(time() - __perftime) + 's to execute')


class File():
    def __init__(self, path, parent=None, isdir=None, uid= -1):
        if len(path) > 1:
            path = path.rstrip(os.path.sep)
        if parent is None:
            self.root = self
            self.basepath = os.path.dirname(path)
            self.basename = os.path.basename(path)
        else:
            if os.path.sep in path:
                raise ValueError('non-root filepaths must be direct relative to parent: path: %s, parent: %s' % (path, parent))
            self.root = parent.root
            self.basename = path
        self.uid = uid
        self.parent = parent
        if isdir is None:
            self.isdir = os.path.isdir(os.path.abspath(self.fullpath))
        else:
            self.isdir = isdir

    def __str__(self):
        return self.fullpath

    def __repr__(self):
        return ('%(fp)s%(isdir)s [%(n)s%(x)s] (%(id)s)%(pid)s' %
             {'fp': self.fullpath,
              'isdir': '/' if self.isdir else '',
              'n': self.name,
              'x': self.ext,
              'id': self.uid,
              'pid': ' -> ' + str(self.parent.uid) if self.parent and self.parent.uid > -1 else ''
              })

    @property
    def relpath(self):
        '''this File's path relative to its root'''
        up = self
        components = deque()
        while up != self.root:
            components.appendleft(up.basename)
            up = up.parent
        return os.path.sep.join(components)

    @property
    def fullpath(self):
        '''this file's relpath with leading root path'''
        fp = os.path.join(self.root.basepath, self.root.basename, self.relpath)
        if len(fp) > 1:
            fp = fp.rstrip(os.path.sep)
        return fp

    @property
    def name(self):
        '''if this file.isdir, its complete basename; otherwise its basename
        without extension suffix'''
        if self.isdir:
            name = self.basename
        else:
            name = os.path.splitext(self.basename)[0]
        return name

    @property
    def ext(self):
        '''if this file.isdir, the empty string; otherwise the extension suffix
        of its basename'''
        if self.isdir:
            ext = ''
        else:
            ext = os.path.splitext(self.basename)[1]
        return ext

    @property
    def exists(self):
        '''True if this file's fullpath exists in the filesystem'''
        return os.path.exists(self.fullpath)

    @property
    def islink(self):
        '''True if this file is a symbolic link'''
        return os.path.islink(self.fullpath)

    def children(self, sort=True, reverse=True):
        '''If self.isdir and self.exists, return an iterable of fileobjects 
        corresponding to its direct content (non-recursive).
        Otherwise, log a warning and return ().
        '''
        try:
            content = os.listdir(self.fullpath)
            if sort:
                content = sorted(content, reverse=reverse)
            return (File(name, parent=self) for name in content)
        except OSError as error:
            log.w('cannot listdir: %s', error)
            return ()


    @classmethod
    def inputfilter(cls, files_iter):
        basedir = cherry.config.media.basedir.str
        for f in files_iter:
            if not f.exists:
                log.e('file not found: ' + f.fullpath + ' . skipping.')
                continue
            if not f.fullpath.startswith(basedir):
                log.e('file not in basedir: ' + f.fullpath + ' . skipping.')
                continue
            if f.islink:
                rp = os.path.realpath(f.fullpath)
                if os.path.abspath(basedir).startswith(rp) \
                    or (os.path.islink(basedir)
                        and
                        os.path.realpath(basedir).startswith(rp)):
                    log.e("Cyclic symlink found: " + f.relpath +
                          " creates a circle if followed. Skipping.")
                    continue
                if not (f.parent is None or f.parent.parent is None):
                    log.e("Deeply nested symlink found: " + f.relpath +
                          " All links must be directly in your basedir (" +
                          os.path.abspath(basedir) + "). The program cannot"
                          " safely handle them otherwise. Skipping.")
                    continue
            yield f