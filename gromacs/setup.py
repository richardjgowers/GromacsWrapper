# $Id$
"""
:mod:`gromacs.setup` -- Setting up a Gromacs MD run
===================================================

Individual steps such as solvating a structure or energy minimization
are set up in individual directories. For energy minimization one
should supply appropriate mdp run input files; otherwise example
templates are used.

.. warning::

   You **must** check all simulation parameters for yourself. Do not rely on
   any defaults provided here. The scripts provided here are provided under the
   assumption that you know what you are doing and you just want to automate
   the boring parts of the process.


User functions
--------------

The individual steps of setting up a simple MD simulation are broken down in a
sequence of functions that depend on the previous step(s):

  :func:`topology`
        generate initial topology file (limited functionality, might require
        manual setup)
  :func:`solvate`
        solvate globular protein and add ions to neutralize
  :func:`energy_minimize`
        set up energy minimization and run it (using ``mdrun_d``)
  :func:`MD_restrained`
        set up restrained MD
  :func:`MD`
        set up equilibrium MD

Each function uses its own working directory (set with the ``dirname`` keyword
argument, but it should be safe and convenient to use the defaults). Other
arguments assume the default locations so typically not much should have to be
set manually.

One can supply non-standard itp files in the topology director. In
some cases one does not use the :func:`topology` function at all but
sets up the topology manually. In this case it is safest to call the
topology directory ``top`` and make sure that it contains all relevant
top, itp, and pdb files.


Example
-------

Run a single protein in a dodecahedral box of SPC water molecules and
use the GROMOS96 G43a1 force field. We start with the structure in
``protein.pdb``::

  from gromacs.setup import *
  f1 = topology(protein='MyProtein', struct='protein.pdb', ff='G43a1', water='spc', force=True, ignh=True)

Each function returns "interesting" new files in a dictionary in such
a away that it can often be used as input for the next function in the
chain (although in most cases one can get away with the defaults of
the keyword arguments)::

  f2 = solvate(**f1)
  f3 = energy_minimize(**f2)

Now prepare input for a MD run with restraints on the protein::

  MD_restrained(**f3)

Use the files in the directory to run the simulation locally or on a
cluster. You can provide your own template for a queuing system
submission script; see the source code for details.

Once the restraint run has completed, use the last frame as input for
the equilibrium MD::

  MD(struct='MD_POSRES/md.gro', runtime=1e5)

Run the resulting tpr file on a cluster.


Functions
---------

The following functions are provided for the user:

.. autofunction:: topology
.. autofunction:: solvate
.. autofunction:: energy_minimize
.. autofunction:: MD_restrained
.. autofunction:: MD

Helper functions (mainly of use to developers):

.. autofunction:: make_main_index
.. autofunction:: add_mdp_includes
.. autofunction:: _setup_MD

"""

from __future__ import with_statement

__docformat__ = "restructuredtext en"

import os
from os.path import realpath
import errno
import re
import shutil
import warnings

import gromacs
import gromacs.config as config
from gromacs import GromacsError, GromacsFailureWarning, GromacsValueWarning, \
     AutoCorrectionWarning, BadParameterWarning, UsageWarning
import gromacs.cbook
from gromacs.utilities import in_dir


#: Concentration of water at standard conditions in mol/L.
#: density at 25 degrees C and 1 atmosphere pressure: rho = 997.0480 g/L.
#: molecular weight: M = 18.015 g/mol
#: c = n/V = m/(V*M) = rho/M  = 55.345 mol/L

CONC_WATER = 55.345

# XXX: This is not used anywhere at the moment:
# parse this table for a usable data structure (and then put it directly in the docs)
recommended_mdp_table = """\
Table: recommended mdp parameters for different FF
==========   =========  ================
mdp          GROMOS     OPLS-AA
==========   =========  ================
rvdw         1.4        1.0
rlist        1.4 ?      1.0
==========   =========  ================
"""


# TODO:
# - should be part of a class so that we can store the topology etc !!!
#   and also store mainselection
# - full logging would be nice (for provenance)
#   should include the shell commands executed (probably add code to core.Command)

def topology(struct=None, protein='protein',
             top='system.top',  dirname='top', **pdb2gmx_args):
    """Build Gromacs topology files from pdb.

    :Keywords:
       *struct*
           input structure (**required**)
       *protein*
           name of the output files
       *top*
           name of the topology file
       *dirname*
           directory in which the new topology will be stored
       *pdb2gmxargs*
           arguments for ``pdb2gmx`` such as ``ff``, ``water``, ...

    .. note::
       At the moment this function simply runs ``pdb2gmx`` and uses
       the resulting topology file directly. If you want to create
       more complicated topologies and maybe also use additional itp
       files or make a protein itp file then you will have to do this
       manually.
    """

    structure = realpath(struct)

    new_struct = protein + '.pdb'
    posres = protein + '_posres.itp'
    tmp_top = "tmp.top"

    pdb2gmx_args.update({'f': structure, 'o': new_struct, 'p': tmp_top, 'i': posres})

    with in_dir(dirname):
        gromacs.pdb2gmx(**pdb2gmx_args)
        # need some editing  protein_tmp --> system.top and protein.itp
        # here... for the time being we just copy
        shutil.copy(tmp_top, top)
    return {'top': realpath(dirname, top), 'struct': realpath(dirname, new_struct)}

trj_compact_main = gromacs.tools.Trjconv(ur='compact', center=True, boxcenter='tric', pbc='mol',
                                         input=('__main__','system'),
                                         doc="Returns a compact representation of the system centered on the __main__ group")

def make_main_index(struct, selection='"Protein"', ndx='main.ndx', oldndx=None):
    """Make index file with the special groups.

    This routine adds the group __main__ and the group __environment__
    to the end of the index file. __main__ contains what the user
    defines as the *central* and *most important* parts of the
    system. __environment__ is everything else.

    The template mdp file, for instance, uses these two groups for T-coupling.

    These groups are mainly useful if the default groups "Protein" and "Non-Protein"
    are not appropriate. By using symbolic names such as __main__ one
    can keep scripts more general.
        
    :Returns:
      *groups* is a list of dictionaries that describe the index groups. See
      :func:`gromacs.cbook.parse_ndxlist` for details.

    :Arguments:    
      *struct* : filename
        structure (tpr, pdb, gro)    
      *selection* : string
        is a ``make_ndx`` command such as ``"Protein"`` or ``r DRG`` which
        determines what is considered the main group for centering etc. It is
        passed directly to ``make_ndx``.
      *ndx* : string
         name of the final index file
      *oldndx* : string
         name of index file that should be used as a basis; if None
         then the ``make_ndx`` default groups are used.
                  
    This routine is very dumb at the moment; maybe some heuristics will be
    added later as could be other symbolic groups such as __membrane__.
    """

    # pass 1: select
    # empty command '' important to get final list of groups
    rc,out,nothing = gromacs.make_ndx(f=struct, n=oldndx, o=ndx, stdout=False, stderr=True,
                                      input=(selection, '', 'q'))
    groups = gromacs.cbook.parse_ndxlist(out)
    last = len(groups) - 1
    assert last == groups[-1]['nr']
    
    # pass 2:
    # 1) last group is __main__
    # 2) __environment__ is everything else (eg SOL, ions, ...)
    rc,out,nothing = gromacs.make_ndx(f=struct, n=ndx, o=ndx,
                                      stdout=False, stderr=True,
                                      input=('name %d __main__' % last,
                                             '! "__main__"',  # is now group last+1
                                             'name %d __environment__' % (last+1),
                                             '', 'q'))
    return gromacs.cbook.parse_ndxlist(out)


def solvate(struct='top/protein.pdb', top='top/system.top',
            distance=0.9, boxtype='dodecahedron',
            concentration=0, cation='NA+', anion='CL-',
            water='spc',
            ndx = 'main.ndx', mainselection = '"Protein"',
            dirname='solvate',
            **kwargs):
    """Put protein into box, add water, add counter-ions.

    Currently this really only supports solutes in water. If you need
    to embedd a protein in a membrane then you will require more
    sophisticated approaches.

    :Arguments:
      *struct* : filename
          pdb or gro input structure
      *top* : filename
          Gromacs topology
      *distance* : float
          When solvating with water, make the box big enough so that
          at least *distance* nm water are between the solute *struct*
          and the box boundary.
      *boxtype* : string
          Any of the box types supported by :class:`~gromacs.tools.Genbox`.
      *concentration* : float
          Concentration of the free ions in mol/l. Note that counter
          ions are added in excess of this concentration.
      *cation* and *anion* : string
          Molecule names of the ions. This depends on the chosen force field.
      *water* : string
          Name of the water model; one of "spc", "spce", "tip3p",
          "tip4p". This should be appropriate for the chosen force
          field.
      *ndx* : filename
          The name of the custom index file that is produced here.
      *mainselection* : string
          A string that is fed to :class:`~gromacs.tools.Make_ndx` and
          which should select the solute.
      *dirname* : directory name
          Name of the directory in which all files for the solvation stage are stored.

    """

    structure = realpath(struct)
    topology = realpath(top)

    # needed only for the include keyword
    mdp_kwargs = add_mdp_includes(topology)

    if water.lower() in ('spc', 'spce'):
        water = 'spc216'

    # By default, grompp should not choke on a few warnings because at
    # this stage the user cannot do much about it (can be set to any
    # value but keep undocumented...)
    grompp_maxwarn = kwargs.pop('maxwarn',10)

    # should scrub topology but hard to know what to scrub; for
    # instance, some ions or waters could be part of the crystal structure
    
    with in_dir(dirname):
        gromacs.editconf(f=structure, o='boxed.gro', bt=boxtype, d=distance)
        gromacs.genbox(p=topology, cp='boxed.gro', cs=water, o='solvated.gro')

        with open('none.mdp','w') as mdp:
            mdp.write('; empty mdp file\ninclude = %(include)s\n' % mdp_kwargs)            
        qtot = gromacs.cbook.grompp_qtot(f='none.mdp', o='topol.tpr', c='solvated.gro',
                                         p=topology, stdout=False, maxwarn=grompp_maxwarn)
        print "After solvation: total charge qtot = %(qtot)r" % vars()        

        if concentration != 0:
            # target concentration of free ions c ==>
            #    N = N_water * c/c_water
            # add ions for concentration to the counter ions (counter ions are less free)
            #
            # get number of waters (count OW ... works for SPC*, TIP*P water models)
            rc,output,junk = gromacs.make_ndx(f='topol.tpr', o='ow.ndx',
                                              input=('keep 0', 'del 0', 'a OW*', 'name 0 OW', '', 'q'),
                                              stdout=False, stderr=True)
            groups = gromacs.cbook.parse_ndxlist(output)
            gdict = dict([(g['name'], g) for g in groups])   # overkill... 
            N_water = gdict['OW']['natoms']                  # ... but dict lookup is nice
            N_ions = int(N_water * concentration/CONC_WATER) # number of monovalents
        else:
            N_ions = 0

        # neutralize (or try -neutral switch of genion???)
        n_cation = n_anion = 0
        if qtot > 0:
            n_anion = int(abs(qtot))
        elif qtot < 0:
            n_cation = int(abs(qtot))

        n_cation += N_ions
        n_anions += N_ions

        if n_cation != 0 or n_anion != 0:
            gromacs.genion(s='topol.tpr', o='ionized.gro', p=topology,
                           pname=cation, nname=anion, np=n_cation, nn=n_anion,
                           input='SOL')
        else:
            # fake ionized file ... makes it easier to continue without too much fuzz
            try:
                os.unlink( 'ionized.gro')
            except OSError, err:
                if err.errno != errno.ENOENT:
                    raise
            os.symlink('solvated.gro', 'ionized.gro')

        qtot = gromacs.cbook.grompp_qtot(f='none.mdp', o='ionized.tpr', c='ionized.gro',
                                         p=topology, stdout=False, maxwarn=grompp_maxwarn)

        if abs(qtot) > 1e-4:
            warnings.warn("System has non-zero total charge qtot = %(qtot)g e." % vars(),
                          category=BadParameterWarning)

        # make main index
        try:
            make_main_index('ionized.tpr', selection=mainselection, ndx=ndx)
        except GromacsError, err:
            # or should I rather fail here?
            warnings.warn("Failed to make main index file %r ... maybe set mainselection='...'.\n"
                          "The error message was:\n%s\n" % (ndx, str(err)), 
                          category=GromacsFailureWarning)

        try:
            trj_compact_main(f='ionized.gro', s='ionized.tpr', o='compact.pdb', n=ndx)
        except GromacsError, err:
            warnings.warn("Failed to make compact pdb for visualization... pressing on regardless. "
                          "The error message was:\n%s\n" % str(err),
                          category=GromacsFailureWarning)
    return {'qtot': qtot, 
            'struct': realpath(dirname, 'ionized.gro'), 
            'ndx': realpath(dirname, ndx),      # not sure why this is propagated-is it used?
            'mainselection': mainselection,
            }


def check_mdpargs(d):
    """Check if any arguments remain in dict *d*."""
    if len(d) > 0:
        warnings.warn("Unprocessed mdp option are interpreted as options for grompp:\n"
                      +str(d),
                      category=UsageWarning)        
    return len(d) == 0

def energy_minimize(dirname='em', mdp=config.templates['em_mdp'],
                    struct='solvate/ionized.gro', top='top/system.top',
                    **kwargs):
    """Energy minimize the system.

    This sets up the system (creates run input files) and also runs
    ``mdrun_d``. Thus it can take a while.

    Additional itp files should be in the same directory as the top file.

    Many of the keyword arguments below already have sensible values. 

    :Keywords:
       *dirname*
          set up under directory dirname [em]
       *struct*
          input structure (gro, pdb, ...) [solvate/ionized.gro]
       *top*
          topology file [top/system.top]
       *mdp*
          mdp file (or use the template) [templates/em.mdp]
       *kwargs*
          remaining key/value pairs that should be changed in the 
          template mdp file, eg ``nstxtcout=250, nstfout=250``.

    .. note:: 
       If ``mdrun_d`` is not found, the function simply fails with
       :exc:`OSError`; in the future it will fall back to ``mdrun`` instead.    
    """

    structure = realpath(struct)
    topology = realpath(top)
    mdp_template = config.get_template(mdp)

    # filter some kwargs that might come through when feeding output
    # from previous stages such as solvate(); necessary because *all*
    # **kwargs must be *either* substitutions in the mdp file *or* valid
    # command line parameters for ``grompp``.
    kwargs.pop('ndx', None)
    # mainselection is not used but only passed through; right now we
    # set it to the default that is being used in all argument lists
    # but that is not pretty. TODO.    
    mainselection = kwargs.pop('mainselection', '"Protein"')
    # only interesting when passed from solvate()
    qtot = kwargs.pop('qtot', 0)

    mdp = 'em.mdp'
    tpr = 'em.tpr'

    add_mdp_includes(topology, kwargs)

    if qtot != 0:
        # At the moment this is purely user-reported and really only here because 
        # it might get fed into the function when using the keyword-expansion pipeline
        # usage paradigm.
        warnings.warn("Total charge was reported as qtot = %(qtot)g <> 0; probably a problem." % vars(),
                      category=BadParameterWarning)

    with in_dir(dirname):
        unprocessed = gromacs.cbook.edit_mdp(mdp_template, new_mdp=mdp, **kwargs)
        check_mdpargs(unprocessed)
        gromacs.grompp(f=mdp, o=tpr, c=structure, p=topology, **unprocessed)
        mdrun_args = dict(v=True, stepout=10, deffnm='em', c='em.pdb')
        # TODO: not clear yet how to run em as MPI with mpiexec & friends
        try:            
            gromacs.mdrun_d(**mdrun_args)
        except (AttributeError, OSError):
            # fall back to mdrun if no double precision binary
            warnings.warn("No 'mdrun_d' binary found so trying 'mdrun' instead.\n"
                          "(Note that energy minimization runs better with mdrun_d.)",
                          category=AutoCorrectionWarning)
            gromacs.mdrun(**mdrun_args)
        # em.gro --> gives 'Bad box in file em.gro' warning --- why??
        # --> use em.pdb instead.
        if not os.path.exists('em.pdb'):
            raise GromacsError("Energy minimized system NOT produced.")
        final_struct = realpath('em.pdb')

    return {'struct': final_struct, 'top': topology,
            'mainselection': mainselection}

def add_mdp_includes(topology, mdp_kwargs=None):
    """Add the directory containing *topology* to the dict *mdp_kwargs*.

    By default, the directories ``.`` and ``..`` are also added to the
    *include* list for the mdp; when fed into
    :func:`gromacs.cbook.edit_mdp` it will result in a line such as ::

      include = -I. -I.. -I../topology_dir

    Note that the user can always override the behaviour by setting
    the *include* keyword herself.

    If no *mdp_kwargs* were supplied then a dict is generated with the
    single *include* entry.

    :Arguments:
       *topology* : top filename
          Topology file; the name of the enclosing directory is added
          to the include path.
       *mdp_kwargs* : dict
          Optional dictionary of mdp keywords; will be modified in place.
    :Returns: 
       *mdp_kwargs* with the *include* keyword added if it did not
       exist previously; if the keyword already existed, nothing
       happens.

    .. Note:: This function is a bit of a hack. It might be removed
              once all setup functions become methods in a nice class.
    """
    if mdp_kwargs is None:
        mdp_kwargs = {}
    # half-hack: find additional itps in the same directory as the
    # topology; once this is all a class we will NOT deduce the local
    # topology directory but just keep it as a class attribute.
    topology_dir = os.path.dirname(topology)
    include_dirs = ['.', '..', topology_dir]
    mdp_includes = ' '.join(['-I%s' % d for d in include_dirs])
    mdp_kwargs.setdefault('include', mdp_includes)   # modify input in place!
    return mdp_kwargs

def _setup_MD(dirname,
              deffnm='md', mdp=config.templates['md_OPLSAA_mdp'],
              struct=None,
              top='top/system.top', ndx=None,
              mainselection='"Protein"',
              sge=config.sge_template, sgename=None,
              dt=0.002, runtime=1e3, **mdp_kwargs):
    """Generic function to set up a ``mdrun`` MD simulation.

    See the user functions for usage.
    """

    if struct is None:
        raise ValueError('struct must be set to a input structure')
    structure = realpath(struct)
    topology = realpath(top)
    try:
        index = realpath(ndx)
    except AttributeError:  # (that's what realpath(None) throws...) 
        index = None        # None is handled fine below

    sge_template = realpath(sge)
    mdp_template = config.get_template(mdp)

    nsteps = int(float(runtime)/float(dt))

    mdp = deffnm + '.mdp'
    tpr = deffnm + '.tpr'
    mainindex = deffnm + '.ndx'
    final_structure = deffnm + '.gro'     # guess... really depends on templates
    sge = os.path.basename(sge_template)
    if sgename is None:
        sgename = 'GMX_MD'
    elif not re.match('[a-zA-Z]', sgename[0]):
        # fix illegal SGE name
        sgename = 'md_'+sgename
        warnings.warn("Illegal SGE name fixed: new=%r" % sgename, 
                      category=AutoCorrectionWarning)

    mdp_parameters = {'nsteps':nsteps, 'dt':dt}
    mdp_parameters.update(mdp_kwargs)

    add_mdp_includes(topology, mdp_parameters)
    
    with in_dir(dirname):
        # Automatic adjustment of T-coupling groups
        if not (mdp_parameters.get('Tcoupl','').lower() == 'no' or mainselection is None):
            # make index file in almost all cases; with None the user
            # takes FULL control and also has to provide the template or index
            groups = make_main_index(structure, selection=mainselection,
                                     oldndx=index, ndx=mainindex)
            natoms = dict([(g['name'], float(g['natoms'])) for g in groups])
            try:
                x = natoms['__main__']/natoms['__environment__']
            except KeyError:
                x = 0   # force using SYSTEM in code below
                warnings.warn("Missing __main__ and/or __environment__ index group.\n"
                              "This probably means that you have an atypical system. You can "
                              "set mainselection=None and provide your own mdp and index files "
                              "in order to set up temperature coupling.\n"
                              "If no T-coupling is required then set Tcoupl='no'.\n"
                              "For now we will just couple everything to 'System'.",
                              category=AutoCorrectionWarning)
            if x < 0.1:
                # TODO: does not handle properly multiple groups in arglist yet
                tau_t = mdp_parameters.pop('tau_t', 0.1)
                ref_t = mdp_parameters.pop('ref_t', 300)
                # combine all in one T-coupling group
                mdp_parameters['tc-grps'] = 'System'
                mdp_parameters['tau_t'] = tau_t   # this overrides the commandline!
                mdp_parameters['ref_t'] = ref_t   # this overrides the commandline!
                warnings.warn("Size of __main__ is only %.1f%% of __environment__ so "
                              "we use 'System' for T-coupling and ref_t = %g and "
                              "tau_t = %g (can be changed in mdp_parameters).\n"
                              % (x * 100, ref_t, tau_t),
                              category=AutoCorrectionWarning)
            index = realpath(mainindex)
        if mdp_parameters.get('Tcoupl','').lower() == 'no':
            mdp_parameters['tc-grps'] = ""
            mdp_parameters['tau_t'] = ""
            mdp_parameters['ref_t'] = ""
        if mdp_parameters.get('Pcoupl','').lower() == 'no':
            mdp_parameters['tau_p'] = ""
            mdp_parameters['ref_p'] = ""
            mdp_parameters['compressibility'] = ""

        unprocessed = gromacs.cbook.edit_mdp(mdp_template, new_mdp=mdp, **mdp_parameters)
        check_mdpargs(unprocessed)
        gromacs.grompp(f=mdp, p=topology, c=structure, n=index, o=tpr, **unprocessed)
        gromacs.cbook.edit_txt(sge_template, [('^DEFFNM=','md',deffnm), 
                                              ('^#$ -N', 'GMX_MD', sgename)], newname=sge)

    print "All files set up for a run time of %(runtime)g ps "\
        "(dt=%(dt)g, nsteps=%(nsteps)g)" % vars()

    kwargs = { # 'dirname':dirname, 'tpr':tpr,  ## cannot be fed into _setup_MD() again and not used sofar
              'struct': realpath(os.path.join(dirname, final_structure)),      # guess
              'top': topology,
              'ndx': index,            # possibly mainindex
              'sge': sge,
              'mainselection': mainselection}
    kwargs.update(mdp_kwargs)  # return extra mdp args so that one can use them for prod run
    kwargs.pop('define', None) # but make sure that -DPOSRES does not stay...
    return kwargs

def MD_restrained(dirname='MD_POSRES', **kwargs):
    """Set up MD with position restraints.

    Additional itp files should be in the same directory as the top file.

    Many of the keyword arguments below already have sensible values. 

    :Keywords:
       *dirname*
          set up under directory dirname [MD_POSRES]
       *struct*
          input structure (gro, pdb, ...) [em/em.pdb]
       *top*
          topology file [top/system.top]
       *mdp*
          mdp file (or use the template) [templates/md.mdp]
       *deffnm*
          default filename for Gromacs run [md]
       *runtime*
          total length of the simulation in ps [1000]
       *dt*
          integration time step in ps [0.002]
       *sge*
          script to submit to the SGE queuing system; by default
          uses the template :data:`gromacs.config.sge_template`, which can 
          be manually set to another template from :data:`gromacs.config.templates`
       *sgename*
          name to be used for the job in the queuing system [PR_GMX]
       *ndx*
          index file
       *mainselection*
          `` make_ndx`` selection to select main group ["Protein"]
          (If ``None`` then no canonical index file is generated and
          it is the users responsibility to set *tc_grps*,
          *tau_t*, and *ref_t* as keyword arguments.
       *kwargs*
          remaining key/value pairs that should be changed in the template mdp
          file, eg ``nstxtcout=250, nstfout=250`` or command line options for
          ``grompp` such as ``maxwarn=1``.

          In particular one can also set **define** and activate
          whichever position restraints have been coded into the itp
          and top file. For instance one could have

             *define* = "-DPOSRES_MainChain -DPOSRES_LIGAND"

          if these preprocessor constructs exist. Note that there
          **must not be any space between "-D" and the value.**

          By default *define* is set to "-DPOSRES".

    :Returns: a dict that can be fed into :func:`gromacs.setup.MD`
              (but check, just in case, especially if you want to
              change the ``define`` parameter in the mdp file)
    """

    kwargs.setdefault('struct', 'em/em.pdb')
    kwargs.setdefault('sgename', 'PR_GMX')
    kwargs.setdefault('define', '-DPOSRES')
    return _setup_MD(dirname, **kwargs)

def MD(dirname='MD', **kwargs):
    """Set up equilibrium MD.

    Additional itp files should be in the same directory as the top file.

    Many of the keyword arguments below already have sensible values. 

    :Keywords:
       *dirname*
          set up under directory dirname [MD]
       *struct*
          input structure (gro, pdb, ...) [MD_POSRES/md_posres.pdb]
       *top*
          topology file [top/system.top]
       *mdp*
          mdp file (or use the template) [templates/md.mdp]
       *deffnm*
          default filename for Gromacs run [md]
       *runtime*
          total length of the simulation in ps [1000]
       *dt*
          integration time step in ps [0.002]
       *sge*
          script template to submit to the SGE queuing system
       *sgename*
          name to be used for the job in the queuing system [MD_GMX]
       *ndx*
          index file
       *mainselection*
          ``make_ndx`` selection to select main group ["Protein"]
          (If ``None`` then no canonical idenx file is generated and
          it is the users responsibility to set 'tc_grps',
          'tau_t', and 'ref_t' via mdp_kwargs.    
       *kwargs*
          remaining key/value pairs that should be changed in the template mdp
          file, eg ``nstxtcout=250, nstfout=250`` or command line options for
          ``grompp` such as ``maxwarn=1``.

    :Returns: a dict that can be fed into :func:`gromacs.setup.MD`
              (but check, just in case, especially if you want to
              change the ``define`` parameter in the mdp file)
    """

    kwargs.setdefault('struct', 'MD_POSRES/md.gro')
    kwargs.setdefault('sgename', 'MD_GMX')
    return _setup_MD(dirname, **kwargs)



# TODO: autorun (qv MI lipids/setup.pl)
