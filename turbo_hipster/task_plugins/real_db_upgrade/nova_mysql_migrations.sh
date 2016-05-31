#!/bin/bash
#
# Copyright 2013 Rackspace Australia
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.


# $1 is the unique job id
# $2 is the working dir path
# $3 is the path to the git repo path
# $4 is the nova db user
# $5 is the nova db password
# $6 is the nova db name
# $7 is the path to the seed dataset to test against
# $8 is the logging.conf for openstack
# $9 is the pip cache dir

UNIQUE_ID=$1
WORKING_DIR_PATH=$2
GIT_REPO_PATH=$3
DB_USER=$4
DB_PASS=$5
DB_NAME=$6
DATASET_SEED_SQL=$7
LOG_CONF_FILE=$8
PIP_CACHE_DIR=$9

# We also support the following environment variables to tweak our behavour:
#   NOCLEANUP: if set to anything, don't cleanup at the end of the run

pip_requires() {
  # $1 is the branch to use for the upper constraints
  pip install -q mysql-python
  pip install -q eventlet
  requires="tools/pip-requires"
  if [ ! -e $requires ]
  then
    requires="requirements.txt"
  fi

  wget http://git.openstack.org/cgit/openstack/requirements/plain/upper-constraints.txt?h=$1 -O $WORKING_DIR_PATH/upper-constraints.txt
  echo "Install pip requirements from $requires"
  pip install -c $WORKING_DIR_PATH/upper-constraints.txt -r $requires

  # Workaround for old python version on ubuntu-precise
  ubuntu_version=$( lsb_release -r | awk '{ print $2 }' | sed 's/[.]//' )
  if [ $ubuntu_version -eq 1204 ]
  then
    pip install -U "greenlet<0.4.9"
  fi

  echo "Requirements installed"
  pip freeze
}

db_sync() {
  # $1 is the test target (ie branch name)
  # $2 is an (optional) destination version number

  # Create a nova.conf file
  cat - > $WORKING_DIR_PATH/nova-$1.conf <<EOF
[DEFAULT]
sql_connection = mysql://$DB_USER:$DB_PASS@172.16.0.1/$DB_NAME?charset=utf8
log_config = $LOG_CONF_FILE
EOF

  # Silently return git to a known good state (delete untracked files)
  git clean -xfdq

  echo "***** Start DB upgrade to state of $1 *****"
  echo "HEAD of branch under test is:"
  git log -n 1

  echo "Setting up the nova-manage entry point"
  python setup.py -q clean
  python setup.py -q develop
  python setup.py -q install

  # Find where we store db versions
  # TODO(mikal): note this only handles the cell db for now
  versions_path="$GIT_REPO_PATH/nova/db/sqlalchemy/cell_migrations/migrate_repo/versions"
  if [ ! -e $versions_path ]
  then
    versions_path="$GIT_REPO_PATH/nova/db/sqlalchemy/migrate_repo/versions"
  fi

  # Log the migrations present
  echo "Migrations present:"
  ls $versions_path/*.py | sed 's/.*\///' | egrep "^[0-9]+_"

  # Flush innodb's caches
  echo "Restarting mysql"
  sudo service mysql stop
  sudo service mysql start

  echo "MySQL counters before upgrade:"
  mysql -u $DB_USER --password=$DB_PASS $DB_NAME -e "show status like 'innodb%';"

  start_version=`mysql -u $DB_USER --password=$DB_PASS $DB_NAME -e "select * from migrate_version \G" | grep version | sed 's/.*: //'`

  if [ "%$2%" == "%%" ]
  then
    end_version=`ls $versions_path/*.py | sed 's/.*\///' | egrep "^[0-9]+_" | tail -1 | cut -f 1 -d "_"`
  else
    end_version=$2
  fi

  echo "Test will migrate from $start_version to $end_version"
  if [ $end_version -lt $start_version ]
  then
    increment=-1
    end_version=$(( $end_version + 1 ))
  else
    increment=1
    start_version=$(( $start_version + 1))
  fi

  for i in `seq $start_version $increment $end_version`
  do
    set -x
    sudo /sbin/ip netns exec nonet `dirname $0`/nova-manage-wrapper.sh $VENV_PATH --config-file $WORKING_DIR_PATH/nova-$1.conf --verbose db sync --version $i
    manage_exit=$?
    set +x

    echo "MySQL counters after upgrade:"
    mysql -u $DB_USER --password=$DB_PASS $DB_NAME -e "show status like 'innodb%';"

    echo "nova-manage returned exit code $manage_exit"
    if [ $manage_exit -gt 0 ]
    then
      echo "Aborting early"
      exit $manage_exit
    fi
  done

  echo "***** Finished DB upgrade to state of $1 *****"
}

stable_release_db_sync() {
  version=`mysql -u $DB_USER --password=$DB_PASS $DB_NAME -e "select * from migrate_version \G" | grep version | sed 's/.*: //'`
  # Some databases are from Juno
  echo "Schema version is $version"
  if [ $version -lt "280" ]
  then
    echo "Database is from Juno! Upgrade via Kilo"
    git branch -D stable/kilo || true
    git remote update
    git checkout -b stable/kilo
    git reset --hard remotes/origin/stable/kilo
    pip_requires stable/kilo
    db_sync "kilo"

    # TODO(jhesketh): This is a bit of a hack until we update our datasets to
    # have the flavour data migrated. We have to do this before upgrading from
    # set -x
    # echo "MySQL counters before migrate_flavor_data:"
    # mysql -u $DB_USER --password=$DB_PASS $DB_NAME -e "show status like 'innodb%';"
    # sudo /sbin/ip netns exec nonet `dirname $0`/nova-manage-wrapper.sh $VENV_PATH --config-file $WORKING_DIR_PATH/nova-kilo.conf --verbose db migrate_flavor_data --force
    # echo "MySQL counters after migrate_flavor_data:"
    # mysql -u $DB_USER --password=$DB_PASS $DB_NAME -e "show status like 'innodb%';"
    # set +x
  fi

  version=`mysql -u $DB_USER --password=$DB_PASS $DB_NAME -e "select * from migrate_version \G" | grep version | sed 's/.*: //'`
  # Some databases are from Kilo
  echo "Schema version is $version"
  if [ $version -lt "302" ]
  then
    echo "Database is from Kilo! Upgrade via Liberty"
    git branch -D stable/liberty || true
    git remote update
    git checkout -b stable/liberty
    git reset --hard remotes/origin/stable/liberty
    pip_requires stable/liberty
    db_sync "liberty"
  fi

  version=`mysql -u $DB_USER --password=$DB_PASS $DB_NAME -e "select * from migrate_version \G" | grep version | sed 's/.*: //'`
  # Some databases are from Liberty
  echo "Schema version is $version"
  if [ $version -lt "319" ]
  then
    echo "Database is from Liberty! Upgrade via Mitaka"
    git branch -D stable/mitaka || true
    git remote update
    git checkout -b stable/mitaka
    git reset --hard remotes/origin/stable/mitaka
    pip_requires stable/mitaka
    db_sync "mitaka"
  fi

  # TODO(jhesketh): Add in Newton here once released

  # TODO(jhesketh): Make this more DRY and/or automatically match migration
  # numbers to releases.
}

echo "Test running on "`hostname`" as "`whoami`" ("`echo ~`", $HOME)"
echo "To execute this script manually, run this:"
echo "$0 $@"

# Setup the environment
set -x
export PATH=/usr/lib/ccache:$PATH
#export PIP_DOWNLOAD_CACHE=$PIP_CACHE_DIR
#export PIP_INDEX_URL="http://www.rcbops.com/pypi/mirror"
export PIP_INDEX_URL="http://pypi.openstack.org/simple/"
export PIP_EXTRA_INDEX_URL="https://pypi.python.org/simple/"
which pip
pip --version
which virtualenv
virtualenv --version
which mkvirtualenv
set +x

# Restore database to known good state
echo "Loading test database $DB_NAME"
set -x
if ! mysql -u $DB_USER --password=$DB_PASS -e "use $DB_NAME"
then
    mysql -u $DB_USER --password=$DB_PASS -e "create database $DB_NAME"
    mysql -u $DB_USER --password=$DB_PASS $DB_NAME < $DATASET_SEED_SQL
fi
set +x

echo "Build test environment"
cd $GIT_REPO_PATH

echo "Setting up virtual env"
source ~/.bashrc
export WORKON_HOME=/var/lib/turbo-hipster/envs
VENV_PATH=$WORKON_HOME/$UNIQUE_ID
rm -rf $VENV_PATH
source /usr/local/bin/virtualenvwrapper.sh
mkvirtualenv --no-site-packages $UNIQUE_ID
#toggleglobalsitepackages
export PYTHONPATH=$PYTHONPATH:$GIT_REPO_PATH

if [ ! -e $VENV_PATH ]
then
  echo "Error: making the virtual env failed"
  exit 1
fi

stable_release_db_sync

last_stable_version=`mysql -u $DB_USER --password=$DB_PASS $DB_NAME -e "select * from migrate_version \G" | grep version | sed 's/.*: //'`
echo "Schema after stable_release_db_sync version is $last_stable_version"

# Make sure the test DB is up to date with trunk
if [ `git show | grep "^\-\-\-" | grep "migrate_repo/versions" | wc -l` -gt 0 ]
then
  echo "This change alters an existing migration, skipping trunk updates."
else
  echo "Update database to current state of trunk"
  git checkout master
  pip_requires master
  db_sync "trunk"
  git checkout working
fi

# Now run the patchset
echo "Now test the patchset"
pip_requires master
db_sync "patchset"

# =============================================================================
# We used to do downgrade testing, but nova no longer supports it
# https://github.com/openstack/openstack-specs/blob/master/specs/no-downward-sql-migration.rst

# # Determine the schema version
# version=`mysql -u $DB_USER --password=$DB_PASS $DB_NAME -e "select * from migrate_version \G" | grep version | sed 's/.*: //'`
# echo "Schema version is $version"

# echo "Now downgrade all the way back to the last stable version (v$last_stable_version)"
# db_sync "downgrade" $last_stable_version

# # Determine the schema version
# version=`mysql -u $DB_USER --password=$DB_PASS $DB_NAME -e "select * from migrate_version \G" | grep version | sed 's/.*: //'`
# echo "Schema version is $version"

# echo "And now back up to head from the start of trunk"
# db_sync "patchset"
# =============================================================================

# Determine the final schema version
version=`mysql -u $DB_USER --password=$DB_PASS $DB_NAME -e "select * from migrate_version \G" | grep version | sed 's/.*: //'`
echo "Final schema version is $version"

if [ "%$NOCLEANUP%" == "%%" ]
then
  # Cleanup virtual env
  echo "Cleaning up virtual env"
  deactivate
  rmvirtualenv $UNIQUE_ID
fi
